"""A confirmação do pagamento: baixa, mensagem fixa e encerramento (tarefa 6.1)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import (
    BaixaCTNRegistro,
    Base,
    Cliente,
    Cobranca,
    Conversa,
    Mensagem,
    Pagamento,
    Transicao,
)
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixFalso, StatusPix
from agente.pagamento import confirmar
from agente.pagamento.concluir import concluir_pagamentos, contar_sem_concluir
from agente.pagamento.confirmar import ResultadoConfirmacao, confirmar_pagamento
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    FormaHistorico,
    MotivoHumano,
    OrigemMensagem,
    Pendencia,
    StatusCobranca,
    StatusPagamento,
    TipoFixa,
)
from agente.turno.acoes import Acao
from agente.turno.catalogo import DadosFixa, mensagem_fixa
from agente.turno.executar import executar
from agente.turno.orquestrador import Dependencias

from ..conftest import piloto_com

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
PAGO_EM = AGORA - timedelta(minutes=2)
DIVIDA = 10020


@pytest.fixture(autouse=True)
def relogio(monkeypatch):
    monkeypatch.setattr(confirmar, "_agora", lambda: AGORA)


class ChatwootInstavel(ChatwootFalso):
    """O Chatwoot que cai: `fora` derruba toda chamada; `perde_a_resposta` entrega sem responder."""

    def __init__(self):
        super().__init__()
        self.fora = False
        self.perde_a_resposta = False

    def _exigir_no_ar(self):
        if self.fora:
            raise httpx.ConnectError("o Chatwoot não respondeu")

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        self._exigir_no_ar()
        chatwoot_id = super().enviar_mensagem(conversa_id, texto, botoes)
        if self.perde_a_resposta:
            self.perde_a_resposta = False
            raise httpx.ReadTimeout("a resposta do Chatwoot se perdeu")
        return chatwoot_id

    def mensagens_do_agente(self, conversa_id):
        self._exigir_no_ar()
        return super().mensagens_do_agente(conversa_id)

    def nota_privada(self, conversa_id, texto):
        self._exigir_no_ar()
        super().nota_privada(conversa_id, texto)


def _deps(politica, **mudancas):
    return Dependencias(
        politica=piloto_com(politica, "+5583900000001").model_copy(update=mudancas),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=LLMTurnoFalso(),
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


def _pagar(sessao, deps, referencia, valor):
    """A 1ª fase e, em seguida, a 2ª: o que a rotina do Trabalhador faz no ciclo seguinte."""
    resultado = confirmar_pagamento(sessao, deps, referencia, valor, PAGO_EM)
    concluir_pagamentos(sessao, deps, AGORA)
    return resultado


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

    resultado = _pagar(sessao, deps, txid, DIVIDA)

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
    _pagar(sessao, deps, txid, DIVIDA)

    resultado = _pagar(sessao, deps, txid, DIVIDA)

    assert resultado is ResultadoConfirmacao.JA_CONFIRMADO
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert len(deps.ctn.baixas) == 1
    assert _transicoes(sessao) == [EstadoConversa.PAGO, EstadoConversa.ENCERRADO]


def test_referencia_desconhecida_nao_toca_em_nada(sessao, ligada, deps):
    resultado = _pagar(sessao, deps, "nao-existe", DIVIDA)

    assert resultado is ResultadoConfirmacao.DESCONHECIDO
    assert deps.ctn.baixas == [] and deps.chatwoot.chamadas == []
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert ligada.cobranca.status is StatusCobranca.ATIVA


def test_valor_divergente_nao_confirma_e_vira_pendencia_para_o_humano(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    resultado = _pagar(sessao, deps, txid, DIVIDA - 500)

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
    assert nomes == ["etiquetar", "atribuir", "mudar_status", "nota_privada"]
    nota = deps.chatwoot.chamadas[-1][2]
    assert "R$ 95,20" in nota and "R$ 100,20" in nota and txid in nota


def test_valor_divergente_repetido_nao_repete_a_pendencia(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA - 500)
    chamadas = len(deps.chatwoot.chamadas)

    resultado = _pagar(sessao, deps, txid, DIVIDA - 500)

    assert resultado is ResultadoConfirmacao.VALOR_DIVERGENTE
    assert len(deps.chatwoot.chamadas) == chamadas


def test_o_propenso_que_pagou_no_pix_recebe_o_convite_do_cartao(sessao, ligada, deps):
    ligada.cobranca.propenso_cartao = True
    txid = _pix(sessao, ligada, deps)

    _pagar(sessao, deps, txid, DIVIDA)

    [link] = [url for url in deps.ctn.links if url.startswith("cartao")]
    esperada = mensagem_fixa(
        TipoFixa.CONFIRMACAO_CONVITE_CARTAO,
        DadosFixa("Cliente Fictício", DIVIDA, f"https://ctn.invalid/cartao/{link}"),
    )
    [chamada] = _enviadas(deps)
    # O botão de link vai no texto, como o Chatwoot o entrega.
    botao = esperada.botoes[0]
    assert chamada[2:] == (f"{esperada.texto}\n\n{botao.texto}: {botao.valor}", [])


def test_o_propenso_que_pagou_pelo_link_recebe_a_confirmacao_simples(sessao, ligada, deps):
    ligada.cobranca.propenso_cartao = True
    id_externo = _link(sessao, ligada, deps)

    _pagar(sessao, deps, id_externo, DIVIDA)

    [chamada] = _enviadas(deps)
    assert "cadastre o seu cartão" not in chamada[2] and chamada[3] == []
    assert [url for url in deps.ctn.links if url.startswith("cartao")] == []


def test_quem_nao_e_propenso_recebe_a_confirmacao_simples_no_pix(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    _pagar(sessao, deps, txid, DIVIDA)

    [chamada] = _enviadas(deps)
    assert "cadastre o seu cartão" not in chamada[2]


def test_o_link_nao_da_baixa_com_o_padrao_e_o_valor_e_o_do_degrau(sessao, ligada, deps):
    id_externo = _link(sessao, ligada, deps, [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK])

    resultado = _pagar(sessao, deps, id_externo, 9000)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert deps.ctn.baixas == []
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.baixa_ctn_em is None
    assert "R$ 90,00" in deps.chatwoot.enviadas(555)[0]


def test_o_link_da_baixa_quando_o_parametro_manda(sessao, ligada, politica):
    deps = _deps(politica, agente_da_baixa_no_link=True)
    id_externo = _link(sessao, ligada, deps)

    _pagar(sessao, deps, id_externo, DIVIDA)

    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, id_externo)]


def test_a_confirmacao_sai_mesmo_depois_da_6a(sessao, ligada, deps):
    for _ in range(5):
        registrar_mensagem(sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, AGORA)
    registrar_mensagem(sessao, ligada.id, OrigemMensagem.FIXA, "canal", None, False, AGORA)
    txid = _pix(sessao, ligada, deps)
    assert mensagens_no_teto(sessao, ligada.id) == 5

    _pagar(sessao, deps, txid, DIVIDA)

    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.estado is EstadoConversa.ENCERRADO


def test_fora_das_24h_a_confirmacao_vai_por_template(sessao, ligada, deps):
    sessao.scalars(select(Mensagem)).one().momento = AGORA - timedelta(hours=30)
    txid = _pix(sessao, ligada, deps)

    _pagar(sessao, deps, txid, DIVIDA)

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

    _pagar(sessao, deps, txid, DIVIDA)

    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert ligada.motivo_humano is None


def test_o_pagamento_confirmado_deixa_os_outros_pendentes_como_estao(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)

    _pagar(sessao, deps, id_externo, DIVIDA)

    status = {p.referencia: p.status for p in sessao.scalars(select(Pagamento))}
    assert status == {txid: StatusPagamento.PENDENTE, id_externo: StatusPagamento.PAGO}


def test_o_segundo_pagamento_da_mesma_cobranca_nao_da_baixa_nem_manda_confirmacao(
    sessao, ligada, deps
):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA)

    resultado = _pagar(sessao, deps, id_externo, DIVIDA)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1 and len(deps.chatwoot.enviadas(555)) == 1
    assert {p.status for p in sessao.scalars(select(Pagamento))} == {StatusPagamento.PAGO}


def test_sem_mensagem_do_cliente_a_confirmacao_vai_por_template(sessao, conversa, deps):
    txid = _pix(sessao, conversa, deps)

    resultado = _pagar(sessao, deps, txid, DIVIDA)

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

    resultado = _pagar(sessao, deps, txid, DIVIDA)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1
    assert deps.chatwoot.chamadas == [] and deps.disparador.envios == []


def test_o_pix_expirado_que_o_banco_confirma_pago_segue_a_sequencia_normal(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    sessao.scalars(select(Pagamento)).one().status = StatusPagamento.EXPIRADO

    resultado = _pagar(sessao, deps, txid, DIVIDA)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.status, pagamento.pago_em) == (StatusPagamento.PAGO, PAGO_EM)
    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert _pagar(sessao, deps, txid, DIVIDA) is (ResultadoConfirmacao.JA_CONFIRMADO)


def _pendentes(sessao):
    consulta = select(Mensagem).where(Mensagem.envio == EstadoEnvio.PENDENTE)
    return sessao.scalars(consulta).all()


def test_a_1a_fase_so_grava_e_nao_toca_em_nada_externo(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert deps.chatwoot.chamadas == [] and deps.ctn.baixas == [] and deps.disparador.envios == []
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.status, pagamento.baixa_ctn_em) == (StatusPagamento.PAGO, None)
    assert pagamento.pendencias == [Pendencia.BAIXA.value, Pendencia.MENSAGEM.value]
    assert pagamento.proxima_tentativa_em == AGORA
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert _transicoes(sessao) == [EstadoConversa.PAGO, EstadoConversa.ENCERRADO]
    [mensagem] = _pendentes(sessao)
    assert (mensagem.origem, mensagem.conta_no_teto) == (OrigemMensagem.FIXA, False)
    assert "R$ 100,20" in mensagem.texto


def test_com_o_chatwoot_fora_o_pagamento_fica_pago_e_a_rotina_manda_a_confirmacao_depois(
    sessao, ligada, politica
):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    deps.chatwoot.fora = True
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert concluir_pagamentos(sessao, deps, AGORA) == 1

    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.status is StatusPagamento.PAGO
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]  # a baixa não esperou o Chatwoot
    assert pagamento.pendencias == [Pendencia.MENSAGEM.value]
    assert pagamento.tentativas_conclusao == 1
    assert pagamento.proxima_tentativa_em == AGORA + deps.politica.espera_integracao(1)
    assert len(_pendentes(sessao)) == 1

    # Antes da espera nada roda; passada a espera, com o Chatwoot de volta, sai uma só.
    deps.chatwoot.fora = False
    assert concluir_pagamentos(sessao, deps, AGORA + timedelta(seconds=1)) == 0
    assert deps.chatwoot.enviadas(555) == []
    depois = AGORA + deps.politica.espera_integracao(1)
    assert concluir_pagamentos(sessao, deps, depois) == 1
    assert len(deps.chatwoot.enviadas(555)) == 1 and _pendentes(sessao) == []
    assert (pagamento.pendencias, pagamento.proxima_tentativa_em) == ([], None)
    assert concluir_pagamentos(sessao, deps, depois + timedelta(days=1)) == 0
    assert len(deps.chatwoot.enviadas(555)) == 1 and len(deps.ctn.baixas) == 1


def test_com_o_ctn_fora_a_mensagem_sai_e_a_baixa_fica_para_a_rotina(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True

    concluir_pagamentos(sessao, deps, AGORA)

    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.status is StatusPagamento.PAGO and pagamento.baixa_ctn_em is None
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert pagamento.pendencias == [Pendencia.BAIXA.value]

    deps.ctn.fora_do_ar = False
    depois = AGORA + deps.politica.espera_integracao(1)
    concluir_pagamentos(sessao, deps, depois)

    assert deps.ctn.baixas == [("M-0001", DIVIDA, PAGO_EM, txid)]
    assert pagamento.baixa_ctn_em == depois and pagamento.proxima_tentativa_em is None
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_a_resposta_perdida_do_chatwoot_nao_reenvia_a_confirmacao(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.chatwoot.perde_a_resposta = True

    concluir_pagamentos(sessao, deps, AGORA)
    assert len(_pendentes(sessao)) == 1 and len(deps.chatwoot.enviadas(555)) == 1

    concluir_pagamentos(sessao, deps, AGORA + deps.politica.espera_integracao(1))

    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _pendentes(sessao) == []
    mensagem = sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA)).one()
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "falso-1")


@respx.mock
def test_com_o_token_que_nao_le_as_mensagens_a_confirmacao_sai_mesmo_assim(
    sessao, ligada, politica
):
    deps = _deps(politica)
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    base = "https://chatwoot.invalid/api/v1/accounts/7/conversations/555/messages"
    # O Chatwoot real: o token do Agent Bot envia (200) mas não lista (401).
    respx.get(base).mock(return_value=httpx.Response(401))
    envio = respx.post(base).mock(return_value=httpx.Response(200, json={"id": 77}))

    with ChatwootHttp("https://chatwoot.invalid", conta=7, token="segredo") as chatwoot:
        deps.chatwoot = chatwoot
        concluir_pagamentos(sessao, deps, AGORA)

    pagamento = sessao.scalars(select(Pagamento)).one()
    assert envio.call_count == 1
    assert "Recebemos o seu pagamento" in json.loads(envio.calls.last.request.content)["content"]
    assert Pendencia.MENSAGEM.value not in pagamento.pendencias and _pendentes(sessao) == []
    mensagem = sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA)).one()
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "77")


def test_o_webhook_repetido_com_a_conclusao_pendente_nao_refaz_nada(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    repetido = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    concluir_pagamentos(sessao, deps, AGORA)

    assert repetido is ResultadoConfirmacao.JA_CONFIRMADO
    assert len(deps.chatwoot.enviadas(555)) == 1 and len(deps.ctn.baixas) == 1


def test_o_convite_do_cartao_e_pedido_ao_ctn_antes_de_travar_a_cobranca(sessao, ligada, deps):
    ligada.cobranca.propenso_cartao = True
    txid = _pix(sessao, ligada, deps)
    eventos = []

    def capturar(estado):
        comando = estado.statement.compile(dialect=postgresql.dialect()).string
        if comando.rstrip().endswith("FOR NO KEY UPDATE"):
            eventos.append("trava")

    event.listen(sessao, "do_orm_execute", capturar)
    gerar = deps.ctn.gerar_link_cadastro_cartao

    def gerar_registrando(matricula):
        eventos.append("ctn")
        return gerar(matricula)

    deps.ctn.gerar_link_cadastro_cartao = gerar_registrando

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    # Um CTN lento não pode segurar a trava do turno e dos outros webhooks.
    assert eventos == ["ctn", "trava", "trava", "trava"]


@pytest.mark.parametrize("caso", ["repetido", "divergente", "dobro", "fora_das_24h", "pelo_link"])
def test_o_convite_so_e_pedido_quando_a_confirmacao_vai_levar_o_link(sessao, ligada, deps, caso):
    ligada.cobranca.propenso_cartao = True
    txid = _pix(sessao, ligada, deps)
    valor = DIVIDA
    if caso == "repetido":
        confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    elif caso == "divergente":
        valor = DIVIDA - 500
    elif caso == "dobro":
        # A cobrança já foi paga pelo link: o Pix que chega depois é o segundo pagamento (D39).
        id_externo = _link(sessao, ligada, deps)
        confirmar_pagamento(sessao, deps, id_externo, DIVIDA, PAGO_EM)
    elif caso == "fora_das_24h":
        ligada.mensagens[0].momento = AGORA - timedelta(hours=30)
    else:
        txid = _link(sessao, ligada, deps)
    convites = len([url for url in deps.ctn.links if url.startswith("cartao")])

    confirmar_pagamento(sessao, deps, txid, valor, PAGO_EM)

    assert len([url for url in deps.ctn.links if url.startswith("cartao")]) == convites


def test_o_convite_que_falha_cai_para_a_confirmacao_simples_e_nao_barra_o_pagamento(
    sessao, ligada, deps
):
    ligada.cobranca.propenso_cartao = True
    txid = _pix(sessao, ligada, deps)
    deps.ctn.fora_do_ar = True

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    [mensagem] = _pendentes(sessao)
    assert "cadastre o seu cartão" not in mensagem.texto
    assert ligada.cobranca.status is StatusCobranca.PAGA


def test_se_a_janela_fecha_na_espera_a_confirmacao_pendente_sai_por_template(
    sessao, ligada, politica
):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.chatwoot.fora = True
    concluir_pagamentos(sessao, deps, AGORA)

    # O cliente escreveu há uma hora; passadas 25h, só o template pode sair.
    concluir_pagamentos(sessao, deps, AGORA + timedelta(hours=25))

    assert deps.chatwoot.enviadas(555) == []
    [envio] = deps.disparador.envios
    assert envio.template == deps.politica.template_confirmacao
    assert envio.variaveis == {"nome": "Cliente", "valor": "R$ 100,20"}
    assert _pendentes(sessao) == []


def test_a_conclusao_que_nao_sai_vira_erro_no_log_depois_das_tentativas(
    sessao, ligada, deps, caplog
):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    agora = AGORA
    with caplog.at_level("WARNING"):
        for _ in range(deps.politica.integracao_tentativas):
            concluir_pagamentos(sessao, deps, agora)
            agora = sessao.scalars(select(Pagamento)).one().proxima_tentativa_em

    erros = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(erros) == 1 and "conferir à mão" in erros[0].getMessage()
    assert txid not in erros[0].getMessage()


def test_a_confirmacao_trava_conversa_cobranca_e_pagamento_nessa_ordem(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    comandos = []

    def capturar(estado):
        comandos.append(estado.statement.compile(dialect=postgresql.dialect()).string)

    event.listen(sessao, "do_orm_execute", capturar)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    travados = [c for c in comandos if c.rstrip().endswith("FOR NO KEY UPDATE")]
    # A mesma ordem do turno ativo (conversa, cobrança), para os dois não se travarem em ciclo.
    assert [c.split("FROM ")[1].split()[0] for c in travados] == [
        "conversa",
        "cobranca",
        "pagamento",
    ]


@pytest.mark.parametrize("caso", ["atendente_assumiu", "turno_passou_ao_humano"])
def test_a_confirmacao_decide_com_a_conversa_relida_depois_das_travas(tmp_path, politica, caso):
    """O que outra transação grava durante a chamada ao CTN vale: a conversa é relida já travada."""
    fabrica = criar_sessao(f"sqlite:///{(tmp_path / 'trava.db').as_posix()}")
    Base.metadata.create_all(fabrica.kw["bind"])
    deps = _deps(politica)
    with fabrica() as sessao:
        cliente = Cliente(
            matricula_ctn="M-0001", nome="Cliente Fictício", telefone="+5583900000001"
        )
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=DIVIDA,
            propenso_cartao=True,
            campanha="teste",
        )
        conversa = Conversa(
            cobranca=cobranca,
            criada_em=datetime(2026, 10, 22, 12, tzinfo=UTC),
            chatwoot_conversa_id=555,
            estado=EstadoConversa.AGUARDANDO_PAGAMENTO,
        )
        sessao.add(conversa)
        sessao.commit()
        registrar_mensagem(
            sessao,
            conversa.id,
            OrigemMensagem.CLIENTE,
            "oi",
            "cw-1",
            False,
            AGORA - timedelta(hours=1),
        )
        sessao.commit()
        txid = _pix(sessao, conversa, deps)
        sessao.commit()
        conversa_id = conversa.id

    gerar = deps.ctn.gerar_link_cadastro_cartao

    def ctn_lento(matricula):
        with fabrica() as outra:
            if caso == "atendente_assumiu":
                mudancas = {"human_owned": True}
            else:
                mudancas = {
                    "estado": EstadoConversa.ATENDIMENTO_HUMANO,
                    "motivo_humano": MotivoHumano.DECISAO_JEV2,
                }
            outra.execute(update(Conversa).where(Conversa.id == conversa_id).values(**mudancas))
            outra.commit()
        return gerar(matricula)

    deps.ctn.gerar_link_cadastro_cartao = ctn_lento

    with fabrica() as sessao:
        confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
        sessao.commit()

    with fabrica() as sessao:
        pagamento = sessao.scalars(select(Pagamento)).one()
        conversa = sessao.get(Conversa, conversa_id)
        assert Pendencia.AVISO_HUMANO.value in pagamento.pendencias
        assert conversa.estado is EstadoConversa.ENCERRADO and conversa.motivo_humano is None


def _chamadas_de_aviso(deps):
    return [c for c in deps.chatwoot.chamadas if c[0] != "enviar_mensagem"]


def test_o_pix_e_o_link_pagos_avisam_o_atendente_para_devolver(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA)
    assert _chamadas_de_aviso(deps) == []

    resultado = _pagar(sessao, deps, id_externo, DIVIDA)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert len(deps.ctn.baixas) == 1 and len(deps.chatwoot.enviadas(555)) == 1
    assert {p.status for p in sessao.scalars(select(Pagamento))} == {StatusPagamento.PAGO}
    assert [c[0] for c in _chamadas_de_aviso(deps)] == [
        "etiquetar",
        "atribuir",
        "mudar_status",
        "nota_privada",
    ]
    assert ("etiquetar", 555, ["pagamento_duplicado"]) in deps.chatwoot.chamadas
    assert ("mudar_status", 555, "open") in deps.chatwoot.chamadas
    nota = _chamadas_de_aviso(deps)[-1][2]
    assert txid in nota and id_externo in nota and nota.count("R$ 100,20") == 2
    assert ligada.estado is EstadoConversa.ENCERRADO


def test_o_pix_vencido_pago_com_o_novo_tambem_pago_avisa_o_atendente(sessao, ligada, deps):
    velho = _pix(sessao, ligada, deps)
    sessao.scalars(select(Pagamento)).one().status = StatusPagamento.EXPIRADO
    _pix(sessao, ligada, deps)
    novo = list(deps.pix.cobrancas)[-1]
    assert novo != velho
    _pagar(sessao, deps, velho, DIVIDA)

    _pagar(sessao, deps, novo, DIVIDA)

    assert len(deps.ctn.baixas) == 1 and len(deps.chatwoot.enviadas(555)) == 1
    nota = [c for c in deps.chatwoot.chamadas if c[0] == "nota_privada"][0][2]
    assert velho in nota and novo in nota


def test_o_segundo_pagamento_repetido_nao_repete_o_aviso(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA)
    _pagar(sessao, deps, id_externo, DIVIDA)
    chamadas = len(deps.chatwoot.chamadas)

    repetido = _pagar(sessao, deps, id_externo, DIVIDA)

    assert repetido is ResultadoConfirmacao.JA_CONFIRMADO
    assert len(deps.chatwoot.chamadas) == chamadas


def test_o_aviso_do_pagamento_em_dobro_nao_se_perde_com_o_chatwoot_fora(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA)
    deps.chatwoot.fora = True

    _pagar(sessao, deps, id_externo, DIVIDA)

    segundo = sessao.scalars(select(Pagamento).where(Pagamento.referencia == id_externo)).one()
    assert segundo.status is StatusPagamento.PAGO
    assert segundo.pendencias == [Pendencia.AVISO_DUPLICADO.value]
    deps.chatwoot.fora = False
    concluir_pagamentos(sessao, deps, AGORA + deps.politica.espera_integracao(1))
    assert segundo.pendencias == []
    assert [c[0] for c in deps.chatwoot.chamadas if c[0] == "nota_privada"] == ["nota_privada"]


def _fechada_pela_baixa_do_ctn(sessao, cobranca, pago_em, id_externo="b-1"):
    """O que a rotina das baixas deixa quando a baixa chega ao CTN antes do nosso webhook."""
    cobranca.status = StatusCobranca.PAGA
    cobranca.encerrada_motivo = "baixa_externa"
    cobranca.encerrada_em = AGORA
    sessao.add(
        BaixaCTNRegistro(
            id_externo=id_externo,
            matricula=cobranca.cliente.matricula_ctn,
            mes_ref="2026/10",
            valor_centavos=DIVIDA,
            forma=FormaHistorico.OUTRO,
            pago_em=pago_em,
            coletado_em=AGORA,
            cobranca_id=cobranca.id,
            aplicada_em=AGORA,
            efeito="quitou",
        )
    )
    sessao.commit()


def test_o_link_pago_depois_da_baixa_do_ctn_nao_e_pagamento_em_dobro(sessao, ligada, deps):
    link = _link(sessao, ligada, deps)
    _fechada_pela_baixa_do_ctn(sessao, ligada.cobranca, PAGO_EM + timedelta(minutes=1))

    resultado = _pagar(sessao, deps, link, DIVIDA)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    pagamento = sessao.scalars(select(Pagamento).where(Pagamento.referencia == link)).one()
    assert pagamento.status is StatusPagamento.PAGO
    assert pagamento.pago_em == PAGO_EM
    # Sem aviso de dobro, sem nova baixa (o CTN já a tem) e sem nova mensagem: a baixa externa já
    # avisou o cliente.
    assert pagamento.pendencias == []
    assert pagamento.proxima_tentativa_em is None
    assert [c for c in deps.chatwoot.chamadas if c[0] in ("nota_privada", "etiquetar")] == []
    assert _enviadas(deps) == []
    assert ligada.cobranca.encerrada_motivo == "baixa_externa"


def test_o_pix_pago_depois_da_baixa_do_ctn_do_mesmo_instante_tambem_nao_e_em_dobro(
    sessao, ligada, deps
):
    txid = _pix(sessao, ligada, deps)
    _fechada_pela_baixa_do_ctn(sessao, ligada.cobranca, PAGO_EM)

    _pagar(sessao, deps, txid, DIVIDA)

    pagamento = sessao.scalars(select(Pagamento).where(Pagamento.referencia == txid)).one()
    assert pagamento.status is StatusPagamento.PAGO and pagamento.pendencias == []
    assert [c for c in deps.chatwoot.chamadas if c[0] in ("nota_privada", "etiquetar")] == []


def test_o_pagamento_nosso_dias_depois_da_baixa_do_ctn_continua_em_dobro(sessao, ligada, deps):
    # O cliente pagou no boleto há 3 dias e agora pagou o nosso Pix: aí sim há o que devolver.
    txid = _pix(sessao, ligada, deps)
    _fechada_pela_baixa_do_ctn(sessao, ligada.cobranca, PAGO_EM - timedelta(days=3))

    _confirmar = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    pagamento = sessao.scalars(select(Pagamento).where(Pagamento.referencia == txid)).one()
    assert _confirmar is ResultadoConfirmacao.CONFIRMADO
    assert pagamento.pendencias == [Pendencia.AVISO_DUPLICADO.value]


def test_a_baixa_do_ctn_de_outra_cobranca_nao_apaga_o_dobro(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    _fechada_pela_baixa_do_ctn(sessao, ligada.cobranca, PAGO_EM)
    sessao.execute(update(BaixaCTNRegistro).values(cobranca_id=None))
    sessao.commit()

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    pagamento = sessao.scalars(select(Pagamento).where(Pagamento.referencia == txid)).one()
    assert pagamento.pendencias == [Pendencia.AVISO_DUPLICADO.value]


def test_se_o_nosso_pix_ja_tinha_sido_pago_o_link_depois_da_baixa_do_ctn_e_em_dobro(
    sessao, ligada, deps
):
    txid = _pix(sessao, ligada, deps)
    link = _link(sessao, ligada, deps)
    _pagar(sessao, deps, txid, DIVIDA)
    # A rotina das baixas só registra a baixa do mesmo pagamento; com o Pix pago a cobrança já era
    # PAGA pelo webhook, e o motivo da baixa externa nem chega a existir. Forçado aqui só para
    # provar a regra: outro pagamento nosso já pago impede o "mesmo pagamento".
    ligada.cobranca.encerrada_motivo = "baixa_externa"
    sessao.add(
        BaixaCTNRegistro(
            id_externo="b-9",
            matricula=ligada.cobranca.cliente.matricula_ctn,
            mes_ref="2026/10",
            valor_centavos=DIVIDA,
            forma=FormaHistorico.OUTRO,
            pago_em=PAGO_EM,
            coletado_em=AGORA,
            cobranca_id=ligada.cobranca.id,
        )
    )
    sessao.commit()

    confirmar_pagamento(sessao, deps, link, DIVIDA, PAGO_EM)

    segundo = sessao.scalars(select(Pagamento).where(Pagamento.referencia == link)).one()
    assert segundo.pendencias == [Pendencia.AVISO_DUPLICADO.value]


def test_o_valor_divergente_fica_gravado_no_pagamento_mesmo_sem_conversa(sessao, conversa, deps):
    txid = _pix(sessao, conversa, deps)
    sessao.delete(conversa)
    sessao.flush()
    sessao.expire_all()

    resultado = _pagar(sessao, deps, txid, DIVIDA - 500)

    assert resultado is ResultadoConfirmacao.VALOR_DIVERGENTE
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.status, pagamento.divergente_centavos) == (
        StatusPagamento.PENDENTE,
        DIVIDA - 500,
    )
    assert deps.chatwoot.chamadas == []


def test_o_valor_divergente_com_o_chatwoot_fora_sai_do_robo_e_a_nota_vem_depois(
    sessao, ligada, politica
):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    deps.chatwoot.fora = True

    resultado = confirmar_pagamento(sessao, deps, txid, DIVIDA - 500, PAGO_EM)
    concluir_pagamentos(sessao, deps, AGORA)

    assert resultado is ResultadoConfirmacao.VALOR_DIVERGENTE
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.pendencias == [Pendencia.AVISO_DIVERGENTE.value]
    deps.chatwoot.fora = False
    concluir_pagamentos(sessao, deps, AGORA + deps.politica.espera_integracao(1))
    assert pagamento.pendencias == []
    assert [c[0] for c in deps.chatwoot.chamadas].count("nota_privada") == 1


def test_a_confirmacao_com_o_humano_na_conversa_deixa_uma_nota_para_o_atendente(
    sessao, ligada, deps
):
    ligada.estado = EstadoConversa.ATENDIMENTO_HUMANO
    ligada.human_owned = True
    txid = _pix(sessao, ligada, deps)

    _pagar(sessao, deps, txid, DIVIDA)

    [nota] = [c for c in deps.chatwoot.chamadas if c[0] == "nota_privada"]
    assert txid in nota[2] and "R$ 100,20" in nota[2]
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_a_confirmacao_sem_humano_na_conversa_nao_deixa_nota(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)

    _pagar(sessao, deps, txid, DIVIDA)

    assert [c for c in deps.chatwoot.chamadas if c[0] == "nota_privada"] == []


def test_o_pagamento_que_outra_instancia_acabou_de_reagendar_nao_e_refeito(
    sessao, ligada, deps, monkeypatch
):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    sessao.commit()
    scalars = sessao.scalars

    def lista_e_depois_outra_instancia_reagenda(*args, **kwargs):
        resultado = scalars(*args, **kwargs)
        lista = resultado.all()
        sessao.execute(
            update(Pagamento)
            .values(proxima_tentativa_em=AGORA + timedelta(minutes=5))
            .execution_options(synchronize_session=False)
        )
        return SimpleNamespace(all=lambda: lista)

    monkeypatch.setattr(sessao, "scalars", lista_e_depois_outra_instancia_reagenda)

    assert concluir_pagamentos(sessao, deps, AGORA) == 0
    assert deps.ctn.baixas == [] and deps.chatwoot.enviadas(555) == []


def _esgotar(sessao, deps, tentativas=None):
    """Roda a rotina até o fim das tentativas. Devolve o instante da última rodada."""
    agora = sessao.scalars(select(Pagamento)).one().proxima_tentativa_em or AGORA
    for _ in range(tentativas or deps.politica.integracao_tentativas):
        concluir_pagamentos(sessao, deps, agora)
        agora = sessao.scalars(select(Pagamento)).one().proxima_tentativa_em or agora
    return agora


def test_esgotadas_as_tentativas_a_conversa_reabre_com_nota_e_etiqueta(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    antes = len([c for c in deps.chatwoot.chamadas if c[0] != "enviar_mensagem"])
    assert antes == 0

    _esgotar(sessao, deps, deps.politica.integracao_tentativas - 1)
    assert [c for c in deps.chatwoot.chamadas if c[0] != "enviar_mensagem"] == []
    _esgotar(sessao, deps, 1)

    avisos = [c for c in deps.chatwoot.chamadas if c[0] != "enviar_mensagem"]
    assert [c[0] for c in avisos] == ["etiquetar", "atribuir", "mudar_status", "nota_privada"]
    assert avisos[0][2] == ["pagamento_sem_concluir"]
    assert txid in avisos[-1][2] and "BAIXA" in avisos[-1][2]
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.atencao_em is not None
    # A marca do AVISO_CONCLUIDO fica na lista: só roda quando a baixa sair.
    assert pagamento.pendencias == [Pendencia.BAIXA.value, Pendencia.AVISO_CONCLUIDO.value]
    assert contar_sem_concluir(sessao) == 1


def test_o_aviso_de_esgotado_sai_uma_vez_so_e_o_pagamento_sai_da_contagem_ao_concluir(
    sessao, ligada, deps
):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    agora = _esgotar(sessao, deps, deps.politica.integracao_tentativas + 2)

    assert [c[0] for c in deps.chatwoot.chamadas].count("nota_privada") == 1
    assert contar_sem_concluir(sessao) == 1

    deps.ctn.fora_do_ar = False
    concluir_pagamentos(sessao, deps, agora)

    assert len(deps.ctn.baixas) == 1
    assert contar_sem_concluir(sessao) == 0
    # O atendente que foi chamado pelo teto recebe a nota de que concluiu, uma vez só.
    notas = [c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada"]
    assert len(notas) == 2 and notas[1].startswith("Pagamento concluído") and txid in notas[1]
    concluir_pagamentos(sessao, deps, agora + timedelta(hours=2))
    assert [c[0] for c in deps.chatwoot.chamadas].count("nota_privada") == 2


def test_a_nota_de_concluido_com_o_chatwoot_fora_fica_pendente_ate_sair(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    agora = _esgotar(sessao, deps)
    deps.ctn.fora_do_ar = False
    deps.chatwoot.fora = True
    concluir_pagamentos(sessao, deps, agora)
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert len(deps.ctn.baixas) == 1
    assert pagamento.pendencias == [Pendencia.AVISO_CONCLUIDO.value]
    # Só falta a nota ao atendente: o pagamento já está concluído e não conta como parado.
    assert contar_sem_concluir(sessao) == 0

    deps.chatwoot.fora = False
    concluir_pagamentos(sessao, deps, pagamento.proxima_tentativa_em)

    assert pagamento.pendencias == [] and pagamento.proxima_tentativa_em is None
    notas = [c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada"]
    assert notas[-1].startswith("Pagamento concluído")


def _notas_de_concluido(deps):
    return [c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada" and "concluído" in c[2]]


def _chatwoot_volta_e_a_rotina_roda(sessao, deps, agora):
    deps.chatwoot.fora = False
    for _ in range(3):
        concluir_pagamentos(sessao, deps, agora)
        agora = sessao.scalars(select(Pagamento)).one().proxima_tentativa_em or agora


def test_o_pagamento_divergente_nunca_recebe_a_nota_de_concluido(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA - 500, PAGO_EM)
    deps.chatwoot.fora = True
    agora = _esgotar(sessao, deps, deps.politica.integracao_tentativas + 1)

    _chatwoot_volta_e_a_rotina_roda(sessao, deps, agora)

    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.pendencias == [] and deps.ctn.baixas == []
    assert _notas_de_concluido(deps) == []


def test_o_pagamento_em_dobro_nunca_recebe_a_nota_de_concluido(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    id_externo = _link(sessao, ligada, deps)
    _pagar(sessao, deps, id_externo, DIVIDA)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.chatwoot.fora = True
    duplicado = sessao.scalar(select(Pagamento).where(Pagamento.referencia == txid))
    agora = duplicado.proxima_tentativa_em
    for _ in range(deps.politica.integracao_tentativas + 1):
        concluir_pagamentos(sessao, deps, agora)
        agora = duplicado.proxima_tentativa_em or agora

    deps.chatwoot.fora = False
    for _ in range(3):
        concluir_pagamentos(sessao, deps, agora)
        agora = duplicado.proxima_tentativa_em or agora

    assert duplicado.pendencias == [] and deps.ctn.baixas == []
    assert _notas_de_concluido(deps) == []
    assert any(
        "Pagamento em dobro" in c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada"
    )


def test_a_nota_de_concluido_vale_mesmo_quando_o_ultimo_passo_e_outro(sessao, ligada, politica):
    """A baixa sai antes do último passo: a nota de concluído não depende de ser ele o da baixa."""
    deps = _deps(politica)
    ligada.estado = EstadoConversa.ATENDIMENTO_HUMANO
    ligada.human_owned = True
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    nota_privada = deps.chatwoot.nota_privada

    def nota_privada_sem_o_aviso_humano(conversa_id, texto):
        if texto.startswith("Pagamento confirmado: "):
            raise httpx.ConnectError("fora")
        nota_privada(conversa_id, texto)

    deps.ctn.fora_do_ar = True
    deps.chatwoot.nota_privada = nota_privada_sem_o_aviso_humano
    agora = _esgotar(sessao, deps)
    deps.ctn.fora_do_ar = False
    concluir_pagamentos(sessao, deps, agora)
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert len(deps.ctn.baixas) == 1 and _notas_de_concluido(deps) == []
    assert Pendencia.AVISO_HUMANO.value in pagamento.pendencias

    del deps.chatwoot.nota_privada
    concluir_pagamentos(sessao, deps, pagamento.proxima_tentativa_em)

    assert pagamento.pendencias == [] and len(_notas_de_concluido(deps)) == 1


def test_o_pagamento_que_nunca_chamou_o_atendente_conclui_sem_nota(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    concluir_pagamentos(sessao, deps, AGORA)
    deps.ctn.fora_do_ar = False
    concluir_pagamentos(sessao, deps, AGORA + timedelta(hours=2))

    assert len(deps.ctn.baixas) == 1
    assert [c for c in deps.chatwoot.chamadas if c[0] == "nota_privada"] == []


def test_o_aviso_de_esgotado_com_o_chatwoot_fora_fica_pendente_ate_sair(sessao, ligada, politica):
    deps = _deps(politica)
    deps.chatwoot = ChatwootInstavel()
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    deps.ctn.fora_do_ar = True
    deps.chatwoot.fora = True
    agora = _esgotar(sessao, deps)
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.atencao_em is None and Pendencia.AVISO_ESGOTADO.value in pagamento.pendencias

    deps.chatwoot.fora = False
    concluir_pagamentos(sessao, deps, agora)

    assert pagamento.atencao_em is not None
    assert Pendencia.AVISO_ESGOTADO.value not in pagamento.pendencias
    assert [c[0] for c in deps.chatwoot.chamadas].count("nota_privada") == 1
    assert contar_sem_concluir(sessao) == 1


def test_o_pagamento_que_conclui_a_tempo_nao_chama_ninguem(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    concluir_pagamentos(sessao, deps, AGORA)

    assert [c for c in deps.chatwoot.chamadas if c[0] != "enviar_mensagem"] == []
    assert contar_sem_concluir(sessao) == 0
