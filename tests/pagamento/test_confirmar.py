"""A confirmação do pagamento: baixa, mensagem fixa e encerramento (tarefa 6.1)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import Mensagem, Pagamento, Transicao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.integracoes.pix import PixFalso, StatusPix
from agente.pagamento import confirmar
from agente.pagamento.confirmar import ResultadoConfirmacao, confirmar_pagamento
from agente.tipos import (
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    StatusCobranca,
    StatusPagamento,
    TipoFixa,
)
from agente.turno.acoes import Acao
from agente.turno.catalogo import DadosFixa, mensagem_fixa
from agente.turno.executar import executar
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
PAGO_EM = AGORA - timedelta(minutes=2)
DIVIDA = 10020


@pytest.fixture(autouse=True)
def relogio(monkeypatch):
    monkeypatch.setattr(confirmar, "_agora", lambda: AGORA)


def _deps(politica, **mudancas):
    return Dependencias(
        politica=liberar_tudo(politica).model_copy(update=mudancas),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=Modo.ATIVO,
        jev=JevFalso(),
        ctn=CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)]),
        pix=PixFalso(agora=lambda: AGORA),
    )


@pytest.fixture
def deps(politica):
    return _deps(politica)


@pytest.fixture
def ligada(sessao, conversa):
    """Uma conversa que aguarda o pagamento, com o cliente tendo escrito há uma hora."""
    conversa.chatwoot_conversa_id = 555
    conversa.estado = EstadoConversa.AGUARDANDO_PAGAMENTO
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, "oi", "cw-1", False, AGORA - timedelta(hours=1)
    )
    sessao.commit()
    return conversa


def _pix(sessao, conversa, deps):
    executar([Acao.GENERATE_PIX], conversa.cobranca, conversa, deps, AGORA)
    sessao.flush()
    return next(iter(deps.pix.cobrancas))


def _link(sessao, conversa, deps, acoes=(Acao.SEND_CTN_LINK,)):
    executar(list(acoes), conversa.cobranca, conversa, deps, AGORA)
    sessao.flush()
    return next(iter(deps.ctn.links))


def _transicoes(sessao):
    return [t.para for t in sessao.scalars(select(Transicao).order_by(Transicao.id))]


def _enviadas(deps):
    return [c for c in deps.chatwoot.chamadas if c[0] == "enviar_mensagem"]


def test_o_pix_pago_confirma_da_baixa_manda_a_fixa_e_encerra(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    deps.pix.simular_pagamento(txid)
    assert deps.pix.consultar(txid) is StatusPix.PAGO

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.status, pagamento.pago_em) == (StatusPagamento.PAGO, PAGO_EM)
    assert pagamento.baixa_ctn_em == AGORA
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 100,20" in texto and "Cliente Fictício" in texto
    assert _transicoes(sessao) == [EstadoConversa.PAGO, EstadoConversa.ENCERRADO]
    assert ligada.estado is EstadoConversa.ENCERRADO
    mensagem = sessao.scalars(select(Mensagem).order_by(Mensagem.id.desc())).first()
    assert (mensagem.origem, mensagem.conta_no_teto) == (OrigemMensagem.FIXA, False)


def test_o_webhook_repetido_nao_manda_duas_confirmacoes(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.JA_CONFIRMADO
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert len(deps.ctn.baixas) == 1
    assert _transicoes(sessao) == [EstadoConversa.PAGO, EstadoConversa.ENCERRADO]


def test_referencia_desconhecida_nao_toca_em_nada(sessao, ligada, deps):
    resultado = confirmar_pagamento(sessao, deps, "nao-existe", DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.DESCONHECIDO
    assert deps.ctn.baixas == [] and deps.chatwoot.chamadas == []
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert ligada.cobranca.status is StatusCobranca.ATIVA


def test_valor_divergente_nao_confirma_e_vira_pendencia_para_o_humano(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA - 500, PAGO_EM)

    assert resultado is ResultadoConfirmacao.VALOR_DIVERGENTE
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.status, pagamento.pago_em) == (StatusPagamento.PENDENTE, None)
    assert ligada.cobranca.status is StatusCobranca.ATIVA
    assert deps.ctn.baixas == []
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.PAGAMENTO_DIVERGENTE,
    )
    assert deps.chatwoot.enviadas(555) == []
    nomes = [chamada[0] for chamada in deps.chatwoot.chamadas]
    assert nomes == ["nota_privada", "etiquetar", "atribuir", "mudar_status"]
    nota = deps.chatwoot.chamadas[0][2]
    assert "R$ 95,20" in nota and "R$ 100,20" in nota and txid in nota


def test_valor_divergente_repetido_nao_repete_a_pendencia(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA - 500, PAGO_EM)
    chamadas = len(deps.chatwoot.chamadas)

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA - 500, PAGO_EM)

    assert resultado is ResultadoConfirmacao.VALOR_DIVERGENTE
    assert len(deps.chatwoot.chamadas) == chamadas


def test_o_propenso_que_pagou_no_pix_recebe_o_convite_do_cartao(sessao, ligada, deps):
    ligada.cobranca.propenso_cartao = True
    txid = _pix(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    [link] = [url for url in deps.ctn.links if url.startswith("cartao")]
    esperada = mensagem_fixa(
        TipoFixa.CONFIRMACAO_CONVITE_CARTAO,
        DadosFixa("Cliente Fictício", DIVIDA, f"https://ctn.invalid/cartao/{link}", ""),
    )
    [chamada] = _enviadas(deps)
    assert chamada[2:] == (esperada.texto, esperada.botoes)


def test_o_propenso_que_pagou_pelo_link_recebe_a_confirmacao_simples(sessao, ligada, deps):
    ligada.cobranca.propenso_cartao = True
    id_externo = _link(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, id_externo, DIVIDA, PAGO_EM)

    [chamada] = _enviadas(deps)
    assert "cadastre o seu cartão" not in chamada[2] and chamada[3] == []
    assert [url for url in deps.ctn.links if url.startswith("cartao")] == []


def test_quem_nao_e_propenso_recebe_a_confirmacao_simples_no_pix(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    [chamada] = _enviadas(deps)
    assert "cadastre o seu cartão" not in chamada[2]


def test_o_link_nao_da_baixa_com_o_padrao_e_o_valor_e_o_do_degrau(sessao, ligada, deps):
    id_externo = _link(sessao, ligada, deps, [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK])

    resultado = confirmar_pagamento(sessao, deps, id_externo, 7020, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert deps.ctn.baixas == []
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.baixa_ctn_em is None
    assert "R$ 70,20" in deps.chatwoot.enviadas(555)[0]


def test_o_link_da_baixa_quando_o_parametro_manda(sessao, ligada, politica):
    deps = _deps(politica, ctn_baixa_no_link=True)
    id_externo = _link(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, id_externo, DIVIDA, PAGO_EM)

    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, id_externo)]


def test_a_confirmacao_sai_mesmo_depois_da_6a(sessao, ligada, deps):
    for _ in range(5):
        registrar_mensagem(sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, AGORA)
    registrar_mensagem(sessao, ligada.id, OrigemMensagem.FIXA, "canal", None, False, AGORA)
    txid = _pix(sessao, ligada, deps)
    assert mensagens_no_teto(sessao, ligada.id) == 5

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.estado is EstadoConversa.ENCERRADO


def test_fora_das_24h_a_confirmacao_vai_por_template(sessao, ligada, deps):
    sessao.scalars(select(Mensagem)).one().momento = AGORA - timedelta(hours=30)
    txid = _pix(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert deps.chatwoot.enviadas(555) == []
    [envio] = deps.disparador.envios
    assert envio.telefone == "+5583900000001"
    assert envio.template == deps.politica.template_confirmacao
    assert envio.variaveis == {"nome": "Cliente", "valor": "R$ 100,20"}
    ultima = sessao.scalars(select(Mensagem).order_by(Mensagem.id.desc())).first()
    assert (ultima.origem, ultima.conta_no_teto) == (OrigemMensagem.ABERTURA, False)
    assert ligada.estado is EstadoConversa.ENCERRADO


def test_a_conversa_com_o_humano_tambem_da_a_baixa_e_sai(sessao, ligada, deps):
    ligada.estado = EstadoConversa.ATENDIMENTO_HUMANO
    ligada.human_owned = True
    txid = _pix(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert ligada.motivo_humano is None


def test_o_pagamento_confirmado_deixa_os_outros_pendentes_como_estao(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)

    confirmar_pagamento(sessao, deps, id_externo, DIVIDA, PAGO_EM)

    status = {p.referencia: p.status for p in sessao.scalars(select(Pagamento))}
    assert status == {txid: StatusPagamento.PENDENTE, id_externo: StatusPagamento.PAGO}


def test_o_segundo_pagamento_da_mesma_cobranca_nao_manda_nem_da_baixa_de_novo(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    resultado = confirmar_pagamento(sessao, deps, id_externo, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1 and len(deps.chatwoot.enviadas(555)) == 1
    assert {p.status for p in sessao.scalars(select(Pagamento))} == {StatusPagamento.PAGO}


def test_sem_mensagem_do_cliente_a_confirmacao_vai_por_template(sessao, conversa, deps):
    txid = _pix(sessao, conversa, deps)

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1
    assert deps.chatwoot.chamadas == []
    assert [envio.template for envio in deps.disparador.envios] == [
        deps.politica.template_confirmacao
    ]
    assert conversa.estado is EstadoConversa.ENCERRADO


def test_sem_conversa_so_confirma_e_da_a_baixa(sessao, conversa, deps):
    txid = _pix(sessao, conversa, deps)
    sessao.delete(conversa)
    sessao.flush()
    sessao.expire_all()

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1
    assert deps.chatwoot.chamadas == [] and deps.disparador.envios == []
