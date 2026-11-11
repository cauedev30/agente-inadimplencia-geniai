"""Demonstração da PARADA 3, com as falsas: o webhook, o laço de turnos e os gates juntos."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.politica import Regua
from agente.tipos import EstadoConversa, OrigemMensagem
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.orquestrador import Dependencias, criar_processador

from .conftest import piloto_com
from .turno.auxiliares import _bruto

SABADO_13H = datetime(2026, 10, 31, 16, tzinfo=UTC)  # 13h em João Pessoa: janela fechada
TERCA_8H = datetime(2026, 11, 3, 11, tzinfo=UTC)  # 8h, depois do Finados
SEGUNDA_10H = datetime(2026, 10, 26, 13, tzinfo=UTC)
TELEFONE = "+5583999990001"


class Relogio:
    def __init__(self, agora):
        self.agora = agora

    def __call__(self):
        return self.agora


@pytest.fixture
def ambiente(politica):
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-1", nome="Cliente Fictício", telefone=TELEFONE)
        sessao.add(
            Conversa(
                cobranca=Cobranca(
                    cliente=cliente,
                    regua=Regua.R3,
                    mensalidades_abertas=3,
                    divida_centavos=10020,
                    propenso_cartao=False,
                    campanha="outubro",
                )
            )
        )
        sessao.commit()
    chatwoot = ChatwootFalso()
    deps = Dependencias(
        piloto_com(politica, TELEFONE),
        chatwoot,
        DisparadorFalso(),
        time_humano=4,
        llm_turno=LLMTurnoFalso(respostas=[_bruto()]),
    )
    relogio = Relogio(SEGUNDA_10H)
    config = Config(database_url="sqlite://", webhook_segredo="s3gredo")
    app = criar_app(
        fabrica,
        politica,
        config,
        processar=criar_processador(deps),
        relogio=relogio,
        iniciar_trabalhador=False,
    )
    with TestClient(app) as cliente_http:
        yield fabrica, chatwoot, relogio, cliente_http, app.state.trabalhador


def _mensagem(cliente_http, mensagem_id, texto="oi"):
    evento = {
        "event": "message_created",
        "id": mensagem_id,
        "content": texto,
        "message_type": "incoming",
        "conversation": {"id": 555},
        "sender": {"type": "contact", "phone_number": TELEFONE},
    }
    return cliente_http.post("/webhooks/chatwoot/s3gredo", json=evento).json()


def test_mensagem_de_sabado_a_tarde_fica_para_terca(ambiente):
    fabrica, chatwoot, relogio, cliente_http, trabalhador = ambiente
    relogio.agora = SABADO_13H
    assert _mensagem(cliente_http, 1) == {"resultado": "agendado"}

    relogio.agora = SABADO_13H + timedelta(seconds=10)
    assert trabalhador.rodar_uma_vez() == 1  # adiado, sem enviar nada
    assert chatwoot.chamadas == []

    relogio.agora = TERCA_8H
    assert trabalhador.rodar_uma_vez() == 1  # na abertura, o turno roda
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        assert conversa.turno_em is None
        assert conversa.turnos[-1].mensagens_ids


def test_depois_de_5_mensagens_sai_a_6a_fixa_e_o_handoff(ambiente, politica):
    fabrica, chatwoot, relogio, cliente_http, trabalhador = ambiente
    _mensagem(cliente_http, 1)
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        for _ in range(politica.teto_resolucao):
            registrar_mensagem(
                sessao, conversa.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
            )
        sessao.commit()

    relogio.agora = SEGUNDA_10H + timedelta(seconds=10)
    trabalhador.rodar_uma_vez()

    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert [c[0] for c in chatwoot.chamadas][:4] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
    ]
    with fabrica() as sessao:
        assert sessao.scalars(select(Conversa)).one().estado is EstadoConversa.ATENDIMENTO_HUMANO

    # O atendente marca Pendente e o robô reassume.
    evento = {"event": "conversation_status_changed", "id": 555, "status": "pending"}
    resposta = cliente_http.post("/webhooks/chatwoot/s3gredo", json=evento).json()
    assert resposta == {"resultado": "devolvida"}
