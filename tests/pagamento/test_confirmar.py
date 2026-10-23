"""A confirmação do pagamento: baixa, mensagem fixa e encerramento (tarefa 6.1)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import Mensagem, Pagamento, Transicao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.integracoes.pix import PixFalso, StatusPix
from agente.pagamento import confirmar
from agente.pagamento.concluir import concluir_pagamentos
from agente.pagamento.confirmar import ResultadoConfirmacao, confirmar_pagamento
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    Modo,
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
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias

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
    assert nomes == ["nota_privada", "etiquetar", "atribuir", "mudar_status"]
    nota = deps.chatwoot.chamadas[0][2]
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
        DadosFixa("Cliente Fictício", DIVIDA, f"https://ctn.invalid/cartao/{link}", ""),
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

    resultado = _pagar(sessao, deps, id_externo, 7020)

    assert resultado is ResultadoConfirmacao.CONFIRMADO
    assert deps.ctn.baixas == []
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert pagamento.baixa_ctn_em is None
    assert "R$ 70,20" in deps.chatwoot.enviadas(555)[0]


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


def test_o_segundo_pagamento_da_mesma_cobranca_nao_manda_nem_da_baixa_de_novo(sessao, ligada, deps):
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


def test_o_webhook_repetido_com_a_conclusao_pendente_nao_refaz_nada(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    repetido = confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)
    concluir_pagamentos(sessao, deps, AGORA)

    assert repetido is ResultadoConfirmacao.JA_CONFIRMADO
    assert len(deps.chatwoot.enviadas(555)) == 1 and len(deps.ctn.baixas) == 1


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


def test_a_confirmacao_trava_a_cobranca_e_o_pagamento_antes_de_ler(sessao, ligada, deps):
    txid = _pix(sessao, ligada, deps)
    comandos = []

    def capturar(estado):
        comandos.append(estado.statement.compile(dialect=postgresql.dialect()).string)

    event.listen(sessao, "do_orm_execute", capturar)

    confirmar_pagamento(sessao, deps, txid, DIVIDA, PAGO_EM)

    travados = [c for c in comandos if c.rstrip().endswith("FOR UPDATE")]
    assert any("FROM cobranca" in c for c in travados)
    assert any("FROM pagamento" in c for c in travados)
