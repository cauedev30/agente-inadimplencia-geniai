from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import LaudoGravado, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.tipos import BatchJev, EstadoConversa, Modo, MotivoHumano
from agente.webchat.amarrar import amarrar_conversa_webchat, tratar_evento_webchat
from agente.webchat.identidade import cobranca_do_identificador, identificador_do_chatwoot

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
WHATSAPP = 555
WEBCHAT = 777
TIME = 4
BASE = "https://chat.invalid/app/accounts/7/conversations"


@pytest.fixture
def ligada(sessao, conversa):
    conversa.chatwoot_conversa_id = WHATSAPP
    conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
    conversa.motivo_humano = MotivoHumano.TETO
    conversa.degrau_atual = 2
    conversa.cobranca.webchat_verificado_em = AGORA
    turno = Turno(conversa_id=conversa.id, modo=Modo.ATIVO)
    sessao.add(turno)
    sessao.flush()
    sessao.add(
        LaudoGravado(
            turno_id=turno.id,
            batch=BatchJev.JEV1,
            origem="falso",
            pergunta="objecao_valor",
            valor=True,
            probabilidade=0.9,
            confianca=0.8,
        )
    )
    sessao.commit()
    return conversa


def _amarrar(sessao, chatwoot, **extra):
    campos = {"time_id": TIME, "agora": AGORA, "url_conversas": BASE}
    amarrar_conversa_webchat(sessao, chatwoot, WEBCHAT, 1, **{**campos, **extra})


def test_amarra_na_ordem_e_deixa_um_ticket_so(sessao, ligada):
    chatwoot = ChatwootFalso()

    _amarrar(sessao, chatwoot)

    nomes = [(c[0], c[1]) for c in chatwoot.chamadas]
    assert nomes == [
        ("gravar_atributos", WEBCHAT),
        ("nota_privada", WEBCHAT),
        ("etiquetar", WEBCHAT),
        ("atribuir", WEBCHAT),
        ("mudar_status", WEBCHAT),
        ("etiquetar", WHATSAPP),
        ("gravar_atributos", WHATSAPP),
        ("nota_privada", WHATSAPP),
    ]
    chamadas = {(c[0], c[1]): c for c in chatwoot.chamadas}
    assert chamadas[("gravar_atributos", WEBCHAT)][2] == {"cobranca_id": 1}
    assert chamadas[("etiquetar", WEBCHAT)][2] == ["webchat", "teto"]
    assert chamadas[("atribuir", WEBCHAT)][2] == TIME
    assert chamadas[("mudar_status", WEBCHAT)][2] == "open"
    assert chamadas[("etiquetar", WHATSAPP)][2] == ["transferida_webchat"]
    assert chamadas[("gravar_atributos", WHATSAPP)][2] == {
        "transferida_webchat": True,
        "conversa_webchat_id": WEBCHAT,
    }
    assert f"{BASE}/{WEBCHAT}" in chamadas[("nota_privada", WHATSAPP)][2]


def test_a_nota_do_webchat_leva_a_divida_as_propostas_o_laudo_e_o_link(sessao, ligada):
    chatwoot = ChatwootFalso()

    _amarrar(sessao, chatwoot)

    [nota] = [c[2] for c in chatwoot.chamadas if c[0] == "nota_privada" and c[1] == WEBCHAT]
    assert "R$ 100,20" in nota
    assert "3 mensalidade" in nota
    assert "degrau 2" in nota
    assert "objecao_valor" in nota and "p=0.90" in nota
    assert f"{BASE}/{WHATSAPP}" in nota
    assert "+5583900000001" not in nota


def test_sem_time_nao_atribui(sessao, ligada):
    chatwoot = ChatwootFalso()

    _amarrar(sessao, chatwoot, time_id=None)

    assert not [c for c in chatwoot.chamadas if c[0] == "atribuir"]


def test_sem_a_url_a_nota_cita_o_numero_da_conversa(sessao, ligada):
    chatwoot = ChatwootFalso()

    _amarrar(sessao, chatwoot, url_conversas=None)

    [nota] = [c[2] for c in chatwoot.chamadas if c[0] == "nota_privada" and c[1] == WEBCHAT]
    assert f"#{WHATSAPP}" in nota


def test_a_amarracao_repetida_nao_duplica_a_nota(sessao, ligada):
    chatwoot = ChatwootFalso()
    _amarrar(sessao, chatwoot)
    antes = list(chatwoot.chamadas)

    _amarrar(sessao, chatwoot)

    assert chatwoot.chamadas == antes
    assert sum(1 for c in chatwoot.chamadas if c[0] == "nota_privada") == 2


def test_outra_conversa_de_webchat_da_mesma_cobranca_e_amarrada_de_novo(sessao, ligada):
    chatwoot = ChatwootFalso()
    _amarrar(sessao, chatwoot)

    amarrar_conversa_webchat(sessao, chatwoot, 888, 1, time_id=TIME, agora=AGORA)

    assert sum(1 for c in chatwoot.chamadas if c[0] == "nota_privada" and c[1] == 888) == 1
    assert ligada.cobranca.webchat_conversa_id == 888


def test_sem_conversa_do_whatsapp_amarra_so_o_webchat(sessao, conversa):
    chatwoot = ChatwootFalso()

    _amarrar(sessao, chatwoot)

    assert {c[1] for c in chatwoot.chamadas} == {WEBCHAT}
    [nota] = [c[2] for c in chatwoot.chamadas if c[0] == "nota_privada"]
    assert "WhatsApp" in nota


def test_cobranca_inexistente_e_erro(sessao):
    with pytest.raises(ValueError, match="cobrança"):
        amarrar_conversa_webchat(sessao, ChatwootFalso(), WEBCHAT, 99, time_id=None, agora=AGORA)


def test_o_identificador_do_widget_volta_ao_id():
    assert cobranca_do_identificador(identificador_do_chatwoot(42)) == 42
    for ruim in ("cobranca-", "cobranca-x", "42", "cobranca-1; drop", None, 5, "Cobranca-1"):
        assert cobranca_do_identificador(ruim) is None


def _evento(identificador="cobranca-1", inbox=9, evento="conversation_created"):
    return {
        "event": evento,
        "id": WEBCHAT,
        "inbox_id": inbox,
        "account": {"id": 7},
        "meta": {"sender": {"identifier": identificador}},
    }


def _config(**extra):
    return Config(
        database_url="sqlite://", webchat_inbox=9, chatwoot_url="https://chat.invalid",
        chatwoot_conta=7, **extra
    )  # fmt: skip


def test_o_evento_de_conversa_criada_do_webchat_amarra(sessao, ligada, politica):
    chatwoot = ChatwootFalso()

    resultado = tratar_evento_webchat(sessao, chatwoot, _evento(), _config(), politica, TIME, AGORA)

    assert resultado == "amarrada"
    assert ligada.cobranca.webchat_conversa_id == WEBCHAT
    assert any(c[0] == "atribuir" and c[1] == WEBCHAT for c in chatwoot.chamadas)
    assert f"https://chat.invalid/app/accounts/7/conversations/{WHATSAPP}" in str(chatwoot.chamadas)


@pytest.mark.parametrize(
    "evento",
    [
        _evento(inbox=3),
        _evento(identificador="alguem"),
        _evento(identificador="cobranca-99"),
        {"event": "conversation_created", "id": WEBCHAT, "inbox_id": 9},
        _evento(evento="message_created"),
    ],
)
def test_o_evento_de_outra_inbox_ou_sem_cobranca_e_ignorado(sessao, ligada, politica, evento):
    chatwoot = ChatwootFalso()

    resultado = tratar_evento_webchat(sessao, chatwoot, evento, _config(), politica, TIME, AGORA)

    assert resultado == "ignorado"
    assert chatwoot.chamadas == []


def test_sem_a_inbox_do_webchat_configurada_ignora(sessao, ligada, politica):
    config = Config(database_url="sqlite://")

    resultado = tratar_evento_webchat(
        sessao, ChatwootFalso(), _evento(), config, politica, TIME, AGORA
    )

    assert resultado == "ignorado"


def test_o_webhook_do_chatwoot_amarra_a_conversa_do_webchat(politica):
    from agente.db.modelos import Base, Cliente, Cobranca, Conversa
    from agente.db.sessao import criar_sessao
    from agente.integracoes.ctn import CTNFalso
    from agente.politica import Regua

    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-1", nome="Fictício", telefone="+5583999990001")
        cobranca = Cobranca(
            cliente=cliente, regua=Regua.R3, mensalidades_abertas=3, divida_centavos=10020,
            propenso_cartao=False, campanha="x",
        )  # fmt: skip
        cobranca.webchat_verificado_em = AGORA
        sessao.add(Conversa(cobranca=cobranca, chatwoot_conversa_id=WHATSAPP))
        sessao.commit()

    chatwoot = ChatwootFalso()

    class Deps:
        time_humano = TIME
        ctn = CTNFalso()

    Deps.chatwoot = chatwoot
    config = _config(webhook_segredo="s3gredo")
    app = criar_app(
        fabrica, politica, config, relogio=lambda: AGORA, iniciar_trabalhador=False,
        dependencias=Deps(),
    )  # fmt: skip
    with TestClient(app) as http:
        resposta = http.post("/webhooks/chatwoot/s3gredo", json=_evento())

    assert resposta.json() == {"resultado": "amarrada"}
    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT


def test_sem_verificacao_do_cpf_o_identifier_forjado_nao_e_amarrado(sessao, ligada, politica):
    ligada.cobranca.webchat_verificado_em = None
    chatwoot = ChatwootFalso()

    resultado = tratar_evento_webchat(sessao, chatwoot, _evento(), _config(), politica, TIME, AGORA)

    assert resultado == "ignorado"
    assert chatwoot.chamadas == []
    assert ligada.cobranca.webchat_conversa_id is None


def test_a_verificacao_velha_nao_vale(sessao, ligada, politica):
    janela = timedelta(minutes=politica.webchat_verificacao_validade_minutos)
    ligada.cobranca.webchat_verificado_em = AGORA - janela - timedelta(seconds=1)
    chatwoot = ChatwootFalso()

    resultado = tratar_evento_webchat(sessao, chatwoot, _evento(), _config(), politica, TIME, AGORA)

    assert resultado == "ignorado"
    assert chatwoot.chamadas == []


def test_a_verificacao_dentro_da_janela_vale(sessao, ligada, politica):
    janela = timedelta(minutes=politica.webchat_verificacao_validade_minutos)
    ligada.cobranca.webchat_verificado_em = AGORA - janela

    resultado = tratar_evento_webchat(
        sessao, ChatwootFalso(), _evento(), _config(), politica, TIME, AGORA
    )

    assert resultado == "amarrada"
