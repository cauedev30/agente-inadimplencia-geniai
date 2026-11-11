"""A PARADA 6 com as falsas: do toque no botão ao pagamento confirmado, pelos webhooks de verdade.

Nada é trocado por monkeypatch além do relógio da confirmação: o toque entra pelo webhook do
Chatwoot, o turno gera o Pix, o `PixFalso.simular_pagamento` paga, o POST assinado em
`/webhooks/pix` (ou `/webhooks/ctn`) chega ao `confirmar_pagamento` real, e o laço do Trabalhador
dá a baixa e envia a confirmação.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import Base, Cliente, Cobranca, Conversa, Mensagem, Pagamento
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixFalso, StatusPix
from agente.pagamento import confirmar
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    OrigemMensagem,
    StatusCobranca,
    StatusPagamento,
)
from agente.turno.acoes import Acao
from agente.turno.executar import executar
from agente.turno.orquestrador import Dependencias, criar_processador

from .conftest import piloto_com
from .turno.auxiliares import _bruto

SEGUNDA_10H = datetime(2026, 10, 26, 13, tzinfo=UTC)  # 10h em João Pessoa: janela aberta
TELEFONE = "+5583999990001"
DIVIDA = 10020
SEGREDOS = {"pix": "segredo-pix", "ctn": "segredo-ctn"}


class Relogio:
    def __init__(self, agora):
        self.agora = agora

    def __call__(self):
        return self.agora


class ChatwootInstavel(ChatwootFalso):
    """O Chatwoot que pode cair: `fora` derruba o envio e a conferência do que já saiu."""

    def __init__(self):
        super().__init__()
        self.fora = False

    def _exigir_no_ar(self):
        if self.fora:
            raise httpx.ConnectError("o Chatwoot não respondeu")

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        self._exigir_no_ar()
        return super().enviar_mensagem(conversa_id, texto, botoes)

    def mensagens_do_agente(self, conversa_id):
        self._exigir_no_ar()
        return super().mensagens_do_agente(conversa_id)


class Ambiente:
    def __init__(self, politica, monkeypatch):
        self.relogio = Relogio(SEGUNDA_10H)
        # A confirmação lê o relógio do módulo; o resto do serviço usa o do app.
        monkeypatch.setattr(confirmar, "_agora", self.relogio)
        self.fabrica = criar_sessao("sqlite://")
        Base.metadata.create_all(self.fabrica.kw["bind"])
        with self.fabrica() as sessao:
            cliente = Cliente(matricula_ctn="F-1", nome="Cliente Fictício", telefone=TELEFONE)
            sessao.add(
                Conversa(
                    cobranca=Cobranca(
                        cliente=cliente,
                        regua=Regua.R3,
                        mensalidades_abertas=3,
                        divida_centavos=DIVIDA,
                        propenso_cartao=False,
                        campanha="outubro",
                    )
                )
            )
            sessao.commit()
        self.chatwoot = ChatwootInstavel()
        self.pix = PixFalso(agora=self.relogio)
        self.ctn = CTNFalso([ClienteCTN("F-1", "Cliente Fictício", TELEFONE, None)])
        self.disparador = DisparadorFalso()
        self.deps = Dependencias(
            piloto_com(politica, TELEFONE),
            self.chatwoot,
            self.disparador,
            time_humano=4,
            llm_turno=LLMTurnoFalso(respostas=[_bruto()]),
            ctn=self.ctn,
            pix=self.pix,
        )
        config = Config(
            database_url="sqlite://",
            webhook_segredo="s3gredo",
            pix_webhook_segredo=SEGREDOS["pix"],
            ctn_webhook_segredo=SEGREDOS["ctn"],
        )
        app = criar_app(
            self.fabrica,
            politica,
            config,
            processar=criar_processador(self.deps),
            relogio=self.relogio,
            iniciar_trabalhador=False,
            dependencias=self.deps,
        )
        self.http = TestClient(app)
        self.trabalhador = app.state.trabalhador

    def tocar_no_botao(self, mensagem_id=1):
        evento = {
            "event": "message_created",
            "id": mensagem_id,
            "content": "",
            "content_attributes": {"payload": BOTAO_PAGAR_PIX.valor},
            "message_type": "incoming",
            "conversation": {"id": 555},
            "sender": {"type": "contact", "phone_number": TELEFONE},
        }
        assert self.http.post("/webhooks/chatwoot/s3gredo", json=evento).json() == {
            "resultado": "agendado"
        }
        self.avancar(timedelta(seconds=10))
        self.trabalhador.rodar_uma_vez()

    def avancar(self, tempo):
        self.relogio.agora += tempo

    def webhook(self, origem, referencia, valor=DIVIDA):
        corpo = json.dumps(
            {
                "referencia": referencia,
                "valor_centavos": valor,
                "pago_em": self.relogio.agora.isoformat(),
            }
        ).encode()
        assinatura = hmac.new(SEGREDOS[origem].encode(), corpo, hashlib.sha256).hexdigest()
        return self.http.post(
            f"/webhooks/{origem}", content=corpo, headers={"X-Assinatura": assinatura}
        )

    def banco(self):
        with self.fabrica() as sessao:
            conversa = sessao.scalars(select(Conversa)).one()
            pagamento = sessao.scalars(select(Pagamento)).one()
            mensagens = sessao.scalars(
                select(Mensagem)
                .where(Mensagem.origem.in_((OrigemMensagem.FIXA, OrigemMensagem.ABERTURA)))
                .order_by(Mensagem.id)
            ).all()
            return (
                conversa.estado,
                conversa.cobranca.status,
                pagamento.status,
                [(m.origem, m.envio) for m in mensagens],
            )


@pytest.fixture
def a(politica, monkeypatch):
    ambiente = Ambiente(politica, monkeypatch)
    with ambiente.http:
        yield ambiente


def test_do_toque_no_botao_ao_pagamento_confirmado_uma_confirmacao_so(a):
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    [texto_pix] = a.chatwoot.enviadas(555)
    assert a.pix.cobrancas[txid].br_code in texto_pix

    a.pix.simular_pagamento(txid)
    assert a.pix.consultar(txid) is StatusPix.PAGO
    pago_em = a.relogio.agora
    resposta = a.webhook("pix", txid)

    assert (resposta.status_code, resposta.json()) == (200, {"resultado": "CONFIRMADO"})
    # O webhook só grava: a conversa já está encerrada, e a confirmação espera o laço.
    assert a.banco() == (
        EstadoConversa.ENCERRADO,
        StatusCobranca.PAGA,
        StatusPagamento.PAGO,
        [(OrigemMensagem.FIXA, EstadoEnvio.PENDENTE)],
    )
    assert a.ctn.baixas == [] and len(a.chatwoot.enviadas(555)) == 1

    a.avancar(timedelta(seconds=1))
    a.trabalhador.rodar_uma_vez()

    [_, confirmacao] = a.chatwoot.enviadas(555)
    assert "Recebemos o seu pagamento de R$ 100,20" in confirmacao
    assert a.ctn.baixas == [("F-1", DIVIDA, pago_em, txid)]
    assert a.banco() == (
        EstadoConversa.ENCERRADO,
        StatusCobranca.PAGA,
        StatusPagamento.PAGO,
        [(OrigemMensagem.FIXA, EstadoEnvio.ENVIADA)],
    )

    # O banco reenvia o webhook e o laço roda de novo: nada sai outra vez.
    assert a.webhook("pix", txid).json() == {"resultado": "JA_CONFIRMADO"}
    a.avancar(timedelta(minutes=5))
    a.trabalhador.rodar_uma_vez()
    assert len(a.chatwoot.enviadas(555)) == 2 and len(a.ctn.baixas) == 1


def test_com_o_chatwoot_fora_o_webhook_responde_200_e_o_laco_envia_depois(a):
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    a.pix.simular_pagamento(txid)
    a.chatwoot.fora = True

    assert a.webhook("pix", txid).status_code == 200
    a.avancar(timedelta(seconds=1))
    a.trabalhador.rodar_uma_vez()

    assert a.banco() == (
        EstadoConversa.ENCERRADO,
        StatusCobranca.PAGA,
        StatusPagamento.PAGO,
        [(OrigemMensagem.FIXA, EstadoEnvio.PENDENTE)],
    )
    assert len(a.ctn.baixas) == 1 and len(a.chatwoot.enviadas(555)) == 1

    a.chatwoot.fora = False
    a.avancar(a.deps.politica.espera_integracao(1))
    a.trabalhador.rodar_uma_vez()
    a.avancar(timedelta(hours=2))
    a.trabalhador.rodar_uma_vez()

    assert len(a.chatwoot.enviadas(555)) == 2  # o Pix e a confirmação, uma vez
    assert len(a.ctn.baixas) == 1
    assert a.banco()[3] == [(OrigemMensagem.FIXA, EstadoEnvio.ENVIADA)]


def test_com_o_ctn_fora_o_pagamento_fica_pago_e_a_baixa_sai_quando_ele_volta(a):
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    a.pix.simular_pagamento(txid)
    a.ctn.fora_do_ar = True

    assert a.webhook("pix", txid).status_code == 200
    a.avancar(timedelta(seconds=1))
    a.trabalhador.rodar_uma_vez()

    assert a.ctn.baixas == [] and len(a.chatwoot.enviadas(555)) == 2
    assert a.banco()[:3] == (EstadoConversa.ENCERRADO, StatusCobranca.PAGA, StatusPagamento.PAGO)

    a.ctn.fora_do_ar = False
    a.avancar(a.deps.politica.espera_integracao(1))
    a.trabalhador.rodar_uma_vez()

    assert len(a.ctn.baixas) == 1 and len(a.chatwoot.enviadas(555)) == 2


def test_depois_das_24h_a_confirmacao_sai_por_template(a):
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    a.pix.simular_pagamento(txid)
    a.avancar(timedelta(hours=26))  # o cliente paga no dia seguinte, em horário de atendimento

    assert a.webhook("pix", txid).status_code == 200
    a.avancar(timedelta(seconds=1))
    a.trabalhador.rodar_uma_vez()

    assert len(a.chatwoot.enviadas(555)) == 1  # só o Pix do toque
    [envio] = a.disparador.envios
    assert envio.template == a.deps.politica.template_confirmacao
    assert envio.telefone == TELEFONE
    assert a.banco()[3] == [(OrigemMensagem.ABERTURA, EstadoEnvio.ENVIADA)]
    assert len(a.ctn.baixas) == 1


def test_o_pagamento_pelo_link_do_ctn_confirma_pelo_webhook_do_ctn(a):
    a.tocar_no_botao()
    with a.fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        executar([Acao.SEND_CTN_LINK], conversa.cobranca, conversa, a.deps, a.relogio.agora)
        sessao.commit()
    [id_externo] = a.ctn.links
    with a.fabrica() as sessao:
        link = sessao.scalars(select(Pagamento).where(Pagamento.referencia == id_externo)).one()
        valor = link.valor_centavos

    resposta = a.webhook("ctn", id_externo, valor)

    assert (resposta.status_code, resposta.json()) == (200, {"resultado": "CONFIRMADO"})
    a.avancar(timedelta(seconds=1))
    a.trabalhador.rodar_uma_vez()
    assert len(a.chatwoot.enviadas(555)) == 2
    assert a.ctn.baixas == []  # o link baixa sozinho no CTN (agente_da_baixa_no_link = false)
    with a.fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        assert (conversa.estado, conversa.cobranca.status) == (
            EstadoConversa.ENCERRADO,
            StatusCobranca.PAGA,
        )


def test_o_webhook_de_uma_forma_nao_confirma_a_referencia_da_outra(a):
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    a.pix.simular_pagamento(txid)

    assert a.webhook("ctn", txid).status_code == 404

    assert a.banco()[:3] == (
        EstadoConversa.AGUARDANDO_PAGAMENTO,
        StatusCobranca.ATIVA,
        StatusPagamento.PENDENTE,
    )
    assert a.webhook("pix", txid).status_code == 200


def test_a_rota_de_saude_dos_pagamentos_conta_os_que_nao_concluem(a):
    assert a.http.get("/saude/pagamentos").json() == {"pagamentos_sem_concluir": 0}
    a.tocar_no_botao()
    [txid] = a.pix.cobrancas
    a.pix.simular_pagamento(txid)
    a.ctn.fora_do_ar = True
    a.webhook("pix", txid)

    for _ in range(a.deps.politica.integracao_tentativas + 1):
        a.avancar(timedelta(hours=2))
        a.trabalhador.rodar_uma_vez()

    assert a.http.get("/saude/pagamentos").json() == {"pagamentos_sem_concluir": 1}
    a.ctn.fora_do_ar = False
    a.avancar(timedelta(hours=2))
    a.trabalhador.rodar_uma_vez()
    assert a.http.get("/saude/pagamentos").json()["pagamentos_sem_concluir"] == 0


def test_o_saude_responde_mesmo_com_o_banco_quebrado(a, monkeypatch):
    def quebrada(*_):
        raise RuntimeError("o banco não responde")

    monkeypatch.setattr(a.fabrica, "__call__", quebrada, raising=False)
    monkeypatch.setattr(type(a.fabrica), "__call__", quebrada)

    assert a.http.get("/saude").json() == {"status": "ok"}
    assert a.http.get("/saude/pagamentos").status_code == 503
