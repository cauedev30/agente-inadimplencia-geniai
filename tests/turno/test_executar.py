"""O backend executa as ações e produz todo número que vai ao cliente (tarefa 5.1)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Pagamento, Transicao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixFalso
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    FormaPagamento,
    OrigemMensagem,
    StatusPagamento,
)
from agente.turno.acoes import Acao
from agente.turno.executar import executar, proximo_estado
from agente.turno.orquestrador import Dependencias, processar_turno

from ..conftest import piloto_com
from .auxiliares import REPLY_LINK, SEGUNDA_10H, TELEFONE, _bruto

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)


def _ctn():
    return CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)])


def _deps(politica, respostas=()):
    return Dependencias(
        politica=piloto_com(politica, TELEFONE),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=LLMTurnoFalso(respostas=list(respostas)),
        ctn=_ctn(),
        pix=PixFalso(agora=lambda: AGORA),
    )


def test_o_desconto_com_o_link_gera_o_link_do_valor_descontado(conversa, politica):
    deps = _deps(politica)
    acoes = [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK]

    numeros = executar(acoes, conversa.cobranca, conversa, deps, AGORA)

    assert (numeros.degrau, numeros.desconto_centavos, numeros.valor_final_centavos) == (
        1,
        3000,
        7020,
    )
    [(id_externo, (matricula, valor, cobranca_id))] = deps.ctn.links.items()
    assert (matricula, valor, cobranca_id) == ("M-0001", 7020, conversa.cobranca.id)
    assert numeros.link_ctn == f"https://ctn.invalid/pagar/{id_externo}"
    assert conversa.degrau_atual == 1
    assert (numeros.forma, conversa.forma_enviada) == (FormaPagamento.LINK_CTN,) * 2


def test_o_link_leva_o_degrau_ja_liberado(conversa, politica):
    conversa.degrau_atual = 2
    deps = _deps(politica)

    numeros = executar(
        [Acao.INFORM_DEBT, Acao.SEND_CTN_LINK], conversa.cobranca, conversa, deps, AGORA
    )

    assert (numeros.divida_centavos, numeros.valor_final_centavos) == (10020, 4020)
    assert [valor for _, valor, _ in deps.ctn.links.values()] == [4020]
    assert conversa.degrau_atual == 2


def test_o_pix_e_sempre_integral_mesmo_com_degrau(conversa, politica):
    conversa.degrau_atual = 2
    deps = _deps(politica)

    numeros = executar([Acao.GENERATE_PIX], conversa.cobranca, conversa, deps, AGORA)

    [pix] = deps.pix.cobrancas.values()
    assert pix.valor_centavos == numeros.valor_pix_centavos == 10020
    assert numeros.br_code == pix.br_code
    assert (numeros.forma, conversa.forma_enviada) == (FormaPagamento.PIX,) * 2


def test_o_vencimento_do_pix_vem_no_fuso_da_janela(conversa, politica):
    numeros = executar([Acao.GENERATE_PIX], conversa.cobranca, conversa, _deps(politica), AGORA)

    assert numeros.pix_vence_em == AGORA + timedelta(hours=politica.pix_validade_horas)
    assert numeros.pix_vence_em.tzinfo == politica.janela.zona


def test_o_pix_grava_o_pagamento_pendente(sessao, conversa, politica):
    deps = _deps(politica)

    executar([Acao.GENERATE_PIX], conversa.cobranca, conversa, deps, AGORA)
    sessao.flush()

    [txid] = deps.pix.cobrancas
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.cobranca_id, pagamento.forma, pagamento.valor_centavos) == (
        conversa.cobranca.id,
        FormaPagamento.PIX,
        10020,
    )
    assert (pagamento.referencia, pagamento.status) == (txid, StatusPagamento.PENDENTE)
    assert pagamento.criado_em == AGORA
    assert pagamento.expira_em == deps.pix.cobrancas[txid].expira_em


def test_o_link_grava_o_pagamento_pendente_com_o_valor_do_degrau(sessao, conversa, politica):
    deps = _deps(politica)

    executar([Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK], conversa.cobranca, conversa, deps, AGORA)
    sessao.flush()

    [id_externo] = deps.ctn.links
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.cobranca_id, pagamento.forma, pagamento.valor_centavos) == (
        conversa.cobranca.id,
        FormaPagamento.LINK_CTN,
        7020,
    )
    assert (pagamento.referencia, pagamento.status) == (id_externo, StatusPagamento.PENDENTE)
    assert pagamento.criado_em == AGORA


def test_o_debito_sozinho_so_traz_a_divida(conversa, politica):
    deps = _deps(politica)

    numeros = executar([Acao.INFORM_DEBT], conversa.cobranca, conversa, deps, AGORA)

    assert (numeros.divida_centavos, numeros.mensalidades, numeros.degrau) == (10020, 3, 0)
    assert (numeros.link_ctn, numeros.br_code, numeros.forma) == (None, None, None)
    assert deps.ctn.links == {} and deps.pix.cobrancas == {}
    assert conversa.forma_enviada is None


def test_desconto_sem_degrau_seguinte_e_erro(conversa, politica):
    # O Luna só recebe o desconto quando há degrau seguinte; se chegar aqui, é bug.
    conversa.degrau_atual = politica.reguas[Regua.R3].n_degraus
    with pytest.raises(ValueError, match="degrau"):
        executar([Acao.OFFER_DISCOUNT], conversa.cobranca, conversa, _deps(politica), AGORA)


def test_os_numeros_viram_json_para_o_turno(conversa, politica):
    numeros = executar(
        [Acao.INFORM_DEBT, Acao.GENERATE_PIX], conversa.cobranca, conversa, _deps(politica), AGORA
    )

    dados = numeros.como_dict()
    assert json.loads(json.dumps(dados)) == dados
    assert dados["acoes"] == ["INFORM_DEBT", "GENERATE_PIX"]
    assert dados["valor_pix_centavos"] == 10020
    assert dados["forma"] == "PIX"
    assert "link_ctn" not in dados


# O turno completo está em test_orquestrador_uma_chamada.


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _cliente(sessao, conversa, texto, mid):
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.CLIENTE, texto, mid, False, SEGUNDA_10H)


OFERTA = [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK)]


def test_no_ativo_o_turno_grava_os_numeros_do_executar(sessao, ligada, politica):
    deps = _deps(politica, respostas=OFERTA)
    _cliente(sessao, ligada, "tá caro", "cw-1")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    turno = ligada.turnos[-1]
    assert turno.numeros["valor_final_centavos"] == 7020
    assert turno.numeros["link_ctn"].startswith("https://ctn.invalid/pagar/")
    assert ligada.degrau_atual == 1
    assert (ligada.estado, ligada.forma_enviada) == (
        EstadoConversa.AGUARDANDO_PAGAMENTO,
        FormaPagamento.LINK_CTN,
    )
    transicao = sessao.scalars(select(Transicao)).one()
    assert transicao.para is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert transicao.laudo_id is not None


@pytest.mark.parametrize(
    ("atual", "forma", "risco", "esperado"),
    [
        (EstadoConversa.INICIADO, None, False, EstadoConversa.ENGAJADO),
        (EstadoConversa.INICIADO, FormaPagamento.PIX, False, EstadoConversa.AGUARDANDO_PAGAMENTO),
        (
            EstadoConversa.ENGAJADO,
            FormaPagamento.LINK_CTN,
            False,
            EstadoConversa.AGUARDANDO_PAGAMENTO,
        ),
        (EstadoConversa.AGUARDANDO_PAGAMENTO, None, False, EstadoConversa.AGUARDANDO_PAGAMENTO),
        (EstadoConversa.EM_RISCO, None, False, EstadoConversa.ENGAJADO),
        (EstadoConversa.EM_RISCO, FormaPagamento.PIX, False, EstadoConversa.AGUARDANDO_PAGAMENTO),
        (EstadoConversa.AGUARDANDO_PAGAMENTO, FormaPagamento.PIX, True, EstadoConversa.EM_RISCO),
    ],
)
def test_o_estado_depois_do_turno(atual, forma, risco, esperado):
    assert proximo_estado(atual, forma, risco) is esperado
