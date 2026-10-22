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
from agente.integracoes.jev import JevFalso, responder
from agente.integracoes.pix import PixFalso
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    FormaPagamento,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusPagamento,
)
from agente.turno.acoes import Acao
from agente.turno.executar import executar, proximo_estado
from agente.turno.jev3 import aprovar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

from .test_orquestrador_gates import SEGUNDA_10H

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)


def _ctn():
    return CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)])


def _deps(politica, modo=Modo.ATIVO, regras=()):
    return Dependencias(
        politica=politica,
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=JevFalso([*regras, *aprovar_tudo()]),
        ctn=_ctn(),
        pix=PixFalso(agora=lambda: AGORA),
    )


def test_o_desconto_com_o_link_gera_o_link_do_valor_descontado(conversa, politica):
    deps = _deps(politica)
    acoes = [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK]

    numeros = executar(acoes, conversa.cobranca, conversa, deps, AGORA, objecao=True)

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


def test_o_debito_sozinho_so_traz_a_divida(conversa, politica):
    deps = _deps(politica)

    numeros = executar([Acao.INFORM_DEBT], conversa.cobranca, conversa, deps, AGORA)

    assert (numeros.divida_centavos, numeros.mensalidades, numeros.degrau) == (10020, 3, 0)
    assert (numeros.link_ctn, numeros.br_code, numeros.forma) == (None, None, None)
    assert deps.ctn.links == {} and deps.pix.cobrancas == {}
    assert conversa.forma_enviada is None


def test_a_objecao_na_regua_sem_desconto_soma_uma(conversa, politica):
    conversa.cobranca.regua = Regua.NR
    acoes = [Acao.INFORM_DEBT, Acao.GENERATE_PIX]

    executar(acoes, conversa.cobranca, conversa, _deps(politica), AGORA, objecao=True)

    assert conversa.objecoes_sem_desconto == 1
    assert conversa.degrau_atual == 0


def test_a_objecao_na_regua_com_desconto_nao_conta(conversa, politica):
    acoes = [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK]

    executar(acoes, conversa.cobranca, conversa, _deps(politica), AGORA, objecao=True)

    assert conversa.objecoes_sem_desconto == 0


def test_sem_objecao_nada_conta(conversa, politica):
    conversa.cobranca.regua = Regua.NR

    executar([Acao.INFORM_DEBT], conversa.cobranca, conversa, _deps(politica), AGORA)

    assert conversa.objecoes_sem_desconto == 0


def test_desconto_sem_degrau_seguinte_e_erro(conversa, politica):
    # O autorizar barra antes; se chegar aqui, é bug.
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


# No turno ativo, o executar roda; em sombra, não. O turno completo está em
# test_orquestrador_ativo.


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _cliente(sessao, conversa, texto, mid):
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.CLIENTE, texto, mid, False, SEGUNDA_10H)


OBJECAO = [responder("objecao_valor", True), responder("acao", "OFFER_DISCOUNT")]


def test_a_2a_objecao_na_nr_chega_ao_humano_no_modo_ativo(sessao, ligada, politica):
    ligada.cobranca.regua = Regua.NR
    deps = _deps(politica, regras=OBJECAO)

    _cliente(sessao, ligada, "tá caro, tem desconto?", "cw-1")
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    assert ligada.objecoes_sem_desconto == 1

    _cliente(sessao, ligada, "sem desconto eu não pago", "cw-2")
    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.OBJECAO_SEM_DESCONTO


def test_no_ativo_o_turno_grava_os_numeros_do_executar(sessao, ligada, politica):
    deps = _deps(politica, regras=OBJECAO)
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


def test_em_sombra_o_executar_nao_roda(sessao, ligada, politica):
    deps = _deps(politica, Modo.SOMBRA, regras=OBJECAO)
    _cliente(sessao, ligada, "tá caro", "cw-1")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert deps.ctn.links == {} and deps.pix.cobrancas == {}
    assert (ligada.degrau_atual, ligada.estado) == (0, EstadoConversa.INICIADO)
    assert sessao.scalars(select(Pagamento)).all() == []


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
