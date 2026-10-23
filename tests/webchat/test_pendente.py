from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import AmarracaoWebchat, Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import CTNFalso
from agente.politica import Regua
from agente.webchat.amarrar import (
    contar_amarracoes_sem_concluir,
    refazer_amarracoes,
    tratar_evento_webchat,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
WHATSAPP = 555
WEBCHAT = 777
TIME = 4


class ChatwootInstavel(ChatwootFalso):
    def __init__(self):
        super().__init__()
        self.fora = False

    def etiquetar(self, conversa_id, etiquetas):
        if self.fora:
            raise httpx.ConnectError("fora do ar")
        super().etiquetar(conversa_id, etiquetas)


def _config(**extra):
    return Config(
        database_url="sqlite://",
        webchat_inbox=9,
        chatwoot_url="https://chat.invalid",
        chatwoot_conta=7,
        webhook_segredo="s3gredo",
        **extra,
    )


def _criada(identificador="cobranca-1"):
    return {
        "event": "conversation_created",
        "id": WEBCHAT,
        "inbox_id": 9,
        "account": {"id": 7},
        "meta": {"sender": {"identifier": identificador}},
    }


def _mensagem(identificador="cobranca-1", **extra):
    evento = {
        "event": "message_created",
        "id": 1000,
        "message_type": "incoming",
        "private": False,
        "content": "oi",
        "account": {"id": 7},
        "inbox": {"id": 9},
        "conversation": {"id": WEBCHAT, "meta": {"sender": {"identifier": identificador}}},
        "sender": {"type": "contact"},
    }
    evento.update(extra)
    return evento


@pytest.fixture
def fabrica():
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-1", nome="Fictício", telefone="+5583999990001")
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="x",
            webchat_verificado_em=AGORA,
        )
        sessao.add(Conversa(cobranca=cobranca, chatwoot_conversa_id=WHATSAPP))
        sessao.commit()
    return fabrica


@pytest.fixture
def sessao(fabrica):
    with fabrica() as sessao:
        yield sessao


def _tratar(sessao, chatwoot, dados, politica, agora=AGORA):
    return tratar_evento_webchat(sessao, chatwoot, dados, _config(), politica, TIME, agora)


def test_conversa_criada_sem_identifier_e_depois_a_mensagem_com_ele_amarra(sessao, politica):
    chatwoot = ChatwootFalso()
    sem_identificador = {**_criada(), "meta": {"sender": {}}}

    assert _tratar(sessao, chatwoot, sem_identificador, politica) == "ignorado"
    assert _tratar(sessao, chatwoot, _mensagem(), politica) == "amarrada"

    assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT
    assert any(c[0] == "nota_privada" and c[1] == WEBCHAT for c in chatwoot.chamadas)


def test_a_mensagem_seguinte_nao_amarra_de_novo(sessao, politica):
    chatwoot = ChatwootFalso()
    _tratar(sessao, chatwoot, _criada(), politica)
    antes = list(chatwoot.chamadas)

    assert _tratar(sessao, chatwoot, _mensagem(), politica) == "ignorado"

    assert chatwoot.chamadas == antes


@pytest.mark.parametrize(
    "mudanca",
    [
        {"message_type": "outgoing"},
        {"private": True},
        {"inbox": {"id": 3}},
        {"sender": {"type": "user"}},
    ],
)
def test_so_a_mensagem_do_cliente_da_inbox_do_webchat_amarra(sessao, politica, mudanca):
    chatwoot = ChatwootFalso()

    assert _tratar(sessao, chatwoot, _mensagem(**mudanca), politica) == "ignorado"
    assert chatwoot.chamadas == []


def test_o_chatwoot_fora_deixa_a_amarracao_pendente_sem_derrubar(sessao, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True

    resultado = _tratar(sessao, chatwoot, _criada(), politica)

    assert resultado == "pendente"
    assert sessao.get(Cobranca, 1).webchat_conversa_id is None
    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
    assert (pendente.cobranca_id, pendente.conversa_webchat_id, pendente.tentativas) == (
        1,
        WEBCHAT,
        1,
    )
    assert pendente.proxima_tentativa_em == AGORA + politica.espera_integracao(1)
    assert pendente.atencao_em is None


def test_a_rotina_refaz_a_pendente_quando_o_chatwoot_volta(sessao, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True
    _tratar(sessao, chatwoot, _criada(), politica)
    chatwoot.fora = False
    espera = politica.espera_integracao(1)

    # Antes do prazo, nada.
    assert refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, AGORA) == 0
    assert sessao.get(Cobranca, 1).webchat_conversa_id is None

    assert refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, AGORA + espera) == 1

    assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT
    assert sessao.scalars(select(AmarracaoWebchat)).all() == []
    assert any(c[0] == "atribuir" and c[1] == WEBCHAT for c in chatwoot.chamadas)


def test_esgotado_o_teto_fica_o_aviso_consultavel_e_a_rotina_segue_tentando(sessao, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True
    _tratar(sessao, chatwoot, _criada(), politica)
    agora = AGORA
    for _ in range(politica.integracao_tentativas - 1):
        [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
        assert contar_amarracoes_sem_concluir(sessao) == 0
        agora = pendente.proxima_tentativa_em
        refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, agora)

    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
    assert pendente.tentativas == politica.integracao_tentativas
    assert pendente.atencao_em == agora
    assert contar_amarracoes_sem_concluir(sessao) == 1

    chatwoot.fora = False
    refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, pendente.proxima_tentativa_em)
    assert contar_amarracoes_sem_concluir(sessao) == 0
    assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT


def test_a_pendente_de_uma_conversa_ja_amarrada_some_sem_chamar_o_chatwoot(sessao, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True
    _tratar(sessao, chatwoot, _criada(), politica)
    sessao.get(Cobranca, 1).webchat_conversa_id = WEBCHAT
    sessao.commit()
    chatwoot.chamadas.clear()

    refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, AGORA + timedelta(days=1))

    assert sessao.scalars(select(AmarracaoWebchat)).all() == []
    assert chatwoot.chamadas == []


def _app(fabrica, politica, chatwoot):
    class Deps:
        time_humano = TIME
        ctn = CTNFalso()

    Deps.chatwoot = chatwoot
    return criar_app(
        fabrica,
        politica,
        _config(),
        relogio=lambda: AGORA,
        iniciar_trabalhador=False,
        dependencias=Deps(),
    )


def test_o_webhook_nao_devolve_500_quando_o_chatwoot_falha(fabrica, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True
    with TestClient(_app(fabrica, politica, chatwoot)) as http:
        resposta = http.post("/webhooks/chatwoot/s3gredo", json=_criada())

    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "pendente"}


def test_o_saude_do_webchat_conta_as_amarracoes_sem_concluir(fabrica, politica):
    with fabrica() as sessao:
        sessao.add(
            AmarracaoWebchat(
                cobranca_id=1,
                conversa_webchat_id=WEBCHAT,
                tentativas=3,
                proxima_tentativa_em=AGORA,
                atencao_em=AGORA,
            )
        )
        sessao.commit()
    with TestClient(_app(fabrica, politica, ChatwootFalso())) as http:
        resposta = http.get("/saude/webchat")

    assert resposta.json() == {"amarracoes_sem_concluir": 1}


def _conversa_criada(conversa_id, identificador="cobranca-1"):
    return {**_criada(identificador), "id": conversa_id}


def test_cada_acerto_do_cpf_vale_uma_conversa_so(sessao, politica):
    chatwoot = ChatwootFalso()
    assert _tratar(sessao, chatwoot, _criada(), politica) == "amarrada"
    assert sessao.get(Cobranca, 1).webchat_verificado_em is None
    antes = list(chatwoot.chamadas)

    forjada = _conversa_criada(999)
    agora = AGORA + timedelta(minutes=30)
    assert _tratar(sessao, chatwoot, forjada, politica, agora) == "ignorado"
    assert _tratar(sessao, chatwoot, _mensagem(conversation={"id": 999}), politica, agora) == (
        "ignorado"
    )

    assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT
    assert chatwoot.chamadas == antes


def test_a_mesma_conversa_repetida_continua_idempotente(sessao, politica):
    chatwoot = ChatwootFalso()
    _tratar(sessao, chatwoot, _criada(), politica)
    antes = list(chatwoot.chamadas)

    assert _tratar(sessao, chatwoot, _mensagem(), politica) == "ignorado"

    assert chatwoot.chamadas == antes


def test_a_verificacao_nova_amarra_a_conversa_nova(sessao, politica):
    chatwoot = ChatwootFalso()
    _tratar(sessao, chatwoot, _criada(), politica)
    sessao.get(Cobranca, 1).webchat_verificado_em = AGORA + timedelta(minutes=10)
    sessao.commit()

    resultado = _tratar(
        sessao, chatwoot, _conversa_criada(999), politica, AGORA + timedelta(minutes=11)
    )

    assert resultado == "amarrada"
    assert sessao.get(Cobranca, 1).webchat_conversa_id == 999


def test_a_pendente_consome_a_verificacao_e_a_forjada_nao_cria_outra(sessao, politica):
    chatwoot = ChatwootInstavel()
    chatwoot.fora = True
    assert _tratar(sessao, chatwoot, _criada(), politica) == "pendente"
    assert sessao.get(Cobranca, 1).webchat_verificado_em is None

    forjada = _conversa_criada(999)
    assert _tratar(sessao, chatwoot, forjada, politica) == "ignorado"
    # A mensagem da própria conversa pendente segue autorizada e espera a rotina.
    assert _tratar(sessao, chatwoot, _mensagem(), politica) == "pendente"

    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
    assert pendente.conversa_webchat_id == WEBCHAT


class ChatwootQueFalhaNoMeio(ChatwootFalso):
    """Falha numa chamada depois das notas, `falhas` vezes."""

    def __init__(self, chamada, falhas):
        super().__init__()
        self.chamada = chamada
        self.falhas = falhas

    def _passar(self, nome):
        if nome == self.chamada and self.falhas > 0:
            self.falhas -= 1
            raise httpx.ConnectError("fora do ar")

    def mudar_status(self, conversa_id, status):
        self._passar("mudar_status")
        super().mudar_status(conversa_id, status)

    def gravar_atributos(self, conversa_id, dados):
        if conversa_id == WHATSAPP:
            self._passar("atributos_origem")
        super().gravar_atributos(conversa_id, dados)


def _notas(chatwoot, conversa):
    return [c for c in chatwoot.chamadas if c[0] == "nota_privada" and c[1] == conversa]


def test_a_falha_depois_da_nota_nao_repete_a_nota_nas_tentativas_seguintes(sessao, politica):
    chatwoot = ChatwootQueFalhaNoMeio("mudar_status", falhas=3)
    assert _tratar(sessao, chatwoot, _criada(), politica) == "pendente"
    agora = AGORA
    for _ in range(2):
        [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
        agora = pendente.proxima_tentativa_em
        refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, agora)
    assert len(_notas(chatwoot, WEBCHAT)) == 1

    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
    assert (
        refazer_amarracoes(
            sessao, chatwoot, _config(), politica, TIME, pendente.proxima_tentativa_em
        )
        == 1
    )

    assert len(_notas(chatwoot, WEBCHAT)) == 1
    assert len(_notas(chatwoot, WHATSAPP)) == 1
    assert sessao.get(Cobranca, 1).webchat_conversa_id == WEBCHAT


def test_a_nota_da_origem_tambem_sai_uma_vez_so(sessao, politica):
    chatwoot = ChatwootQueFalhaNoMeio("atributos_origem", falhas=1)
    assert _tratar(sessao, chatwoot, _criada(), politica) == "pendente"
    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()

    refazer_amarracoes(sessao, chatwoot, _config(), politica, TIME, pendente.proxima_tentativa_em)

    assert len(_notas(chatwoot, WEBCHAT)) == 1
    assert len(_notas(chatwoot, WHATSAPP)) == 1


def test_a_corrida_da_pendente_duplicada_nao_vira_erro(fabrica, sessao, politica):
    class ChatwootComCorrida(ChatwootInstavel):
        """Enquanto esta falha, outro processo grava a pendente da mesma conversa."""

        def etiquetar(self, conversa_id, etiquetas):
            with fabrica() as outra:
                outra.add(
                    AmarracaoWebchat(
                        cobranca_id=1,
                        conversa_webchat_id=WEBCHAT,
                        tentativas=1,
                        proxima_tentativa_em=AGORA + timedelta(minutes=5),
                    )
                )
                outra.commit()
            raise httpx.ConnectError("fora do ar")

    resultado = _tratar(sessao, ChatwootComCorrida(), _criada(), politica)

    assert resultado == "pendente"
    [pendente] = sessao.scalars(select(AmarracaoWebchat)).all()
    assert pendente.tentativas == 2
