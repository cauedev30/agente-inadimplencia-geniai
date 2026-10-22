from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import Base, Cliente, Cobranca, Conversa, Mensagem
from agente.db.sessao import criar_sessao
from agente.politica import Regua
from agente.tipos import OrigemMensagem
from agente.turno.fila import (
    Trabalhador,
    agendar_turno,
    mensagens_pendentes,
    tomar_turno,
    turnos_vencidos,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)  # segunda, 10h em João Pessoa
TELEFONE = "+5583999990001"


class Relogio:
    def __init__(self, agora):
        self.agora = agora

    def __call__(self):
        return self.agora


@pytest.fixture
def fabrica():
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-1", nome="Cliente Fictício", telefone=TELEFONE)
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="outubro",
        )
        # A conversa nasce na abertura, ainda sem o id do Chatwoot.
        sessao.add(Conversa(cobranca=cobranca))
        sessao.commit()
    return fabrica


@pytest.fixture
def relogio():
    return Relogio(AGORA)


@pytest.fixture
def cliente_http(fabrica, politica, relogio):
    config = Config(
        database_url="sqlite://", webhook_segredo="s3gredo", chatwoot_conta=7, chatwoot_inbox=3
    )
    app = criar_app(fabrica, politica, config, relogio=relogio, iniciar_trabalhador=False)
    with TestClient(app) as cliente:
        yield cliente


def _evento(mensagem_id=100, texto="quanto eu devo?", **extra):
    evento = {
        "event": "message_created",
        "id": mensagem_id,
        "content": texto,
        "message_type": "incoming",
        "private": False,
        "account": {"id": 7},
        "inbox": {"id": 3},
        "conversation": {"id": 555, "status": "pending"},
        "sender": {"type": "contact", "phone_number": TELEFONE},
        "attachments": [],
    }
    evento.update(extra)
    return evento


def _postar(cliente_http, evento, segredo="s3gredo"):
    return cliente_http.post(f"/webhooks/chatwoot/{segredo}", json=evento)


def test_segredo_errado_responde_401(cliente_http):
    assert _postar(cliente_http, _evento(), segredo="errado").status_code == 401


def test_json_invalido_responde_400(cliente_http):
    resposta = cliente_http.post(
        "/webhooks/chatwoot/s3gredo",
        content=b"{nao e json",
        headers={"content-type": "application/json"},
    )
    assert resposta.status_code == 400


def test_mensagem_do_cliente_e_gravada_e_agenda_o_turno(cliente_http, fabrica, politica):
    resposta = _postar(cliente_http, _evento())

    assert resposta.json() == {"resultado": "agendado"}
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        assert conversa.chatwoot_conversa_id == 555
        assert conversa.turno_em == AGORA + timedelta(seconds=politica.rajada_segundos)
        mensagem = sessao.scalars(select(Mensagem)).one()
        assert (mensagem.origem, mensagem.texto) == (OrigemMensagem.CLIENTE, "quanto eu devo?")
        assert mensagem.conta_no_teto is False


def test_mensagem_repetida_e_ignorada(cliente_http, fabrica):
    _postar(cliente_http, _evento())

    assert _postar(cliente_http, _evento()).json() == {"resultado": "repetida"}
    with fabrica() as sessao:
        assert len(sessao.scalars(select(Mensagem)).all()) == 1


def test_rajada_vira_um_turno_so(cliente_http, fabrica, relogio):
    for indice in range(3):
        relogio.agora = AGORA + timedelta(seconds=2 * indice)
        _postar(cliente_http, _evento(mensagem_id=100 + indice, texto=f"msg {indice}"))

    with fabrica() as sessao:
        # Antes de 7 s sem mensagem nova, não há turno.
        assert turnos_vencidos(sessao, AGORA + timedelta(seconds=10)) == []
        [conversa_id] = turnos_vencidos(sessao, AGORA + timedelta(seconds=11))
        mensagens = tomar_turno(sessao, conversa_id)
        assert [m.texto for m in mensagens] == ["msg 0", "msg 1", "msg 2"]
        assert turnos_vencidos(sessao, AGORA + timedelta(seconds=60)) == []


def test_conta_ou_inbox_de_outro_lugar_e_ignorada(cliente_http, fabrica):
    assert _postar(cliente_http, _evento(account={"id": 99})).json() == {"resultado": "ignorado"}
    assert _postar(cliente_http, _evento(inbox={"id": 99})).json() == {"resultado": "ignorado"}
    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).all() == []


def test_telefone_sem_cobranca_ativa_e_ignorado(cliente_http):
    evento = _evento(sender={"type": "contact", "phone_number": "+5583900000000"})
    assert _postar(cliente_http, evento).json() == {"resultado": "sem_cobranca"}


def test_anexo_e_gravado_com_o_tipo(cliente_http, fabrica):
    _postar(cliente_http, _evento(texto="", attachments=[{"file_type": "audio"}]))

    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).one().anexos == [{"tipo": "audio"}]


def test_mensagem_do_atendente_marca_humano_na_conversa(cliente_http, fabrica):
    _postar(cliente_http, _evento())
    evento = _evento(
        mensagem_id=200,
        texto="Oi, sou a atendente",
        message_type="outgoing",
        sender={"type": "user"},
    )

    assert _postar(cliente_http, evento).json() == {"resultado": "humano"}
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        assert conversa.human_owned is True
        origens = [m.origem for m in sessao.scalars(select(Mensagem).order_by(Mensagem.id))]
        assert origens == [OrigemMensagem.CLIENTE, OrigemMensagem.HUMANO]


def test_mensagem_do_proprio_robo_e_nota_privada_sao_ignoradas(cliente_http):
    do_robo = _evento(mensagem_id=300, message_type="outgoing", sender={"type": "agent_bot"})
    nota = _evento(mensagem_id=301, message_type="outgoing", private=True, sender={"type": "user"})

    assert _postar(cliente_http, do_robo).json() == {"resultado": "ignorado"}
    assert _postar(cliente_http, nota).json() == {"resultado": "ignorado"}


def test_agendamento_sobrevive_a_recriar_o_app(fabrica, politica, relogio):
    config = Config(database_url="sqlite://", webhook_segredo="s3gredo")
    with TestClient(
        criar_app(fabrica, politica, config, relogio=relogio, iniciar_trabalhador=False)
    ) as c:
        _postar(c, _evento())

    criar_app(fabrica, politica, config, relogio=relogio, iniciar_trabalhador=False)
    with fabrica() as sessao:
        assert len(turnos_vencidos(sessao, AGORA + timedelta(seconds=30))) == 1


def test_mensagens_pendentes_comecam_depois_do_ultimo_turno(fabrica, cliente_http):
    _postar(cliente_http, _evento(mensagem_id=1, texto="primeira"))
    with fabrica() as sessao:
        [conversa_id] = turnos_vencidos(sessao, AGORA + timedelta(seconds=30))
        tomar_turno(sessao, conversa_id)
        sessao.commit()
    _postar(cliente_http, _evento(mensagem_id=2, texto="segunda"))

    with fabrica() as sessao:
        assert [m.texto for m in mensagens_pendentes(sessao, conversa_id)] == ["segunda"]


def test_agendar_turno_empurra_para_frente(fabrica):
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        agendar_turno(sessao, conversa.id, AGORA)
        agendar_turno(sessao, conversa.id, AGORA + timedelta(seconds=5))
        assert conversa.turno_em == AGORA + timedelta(seconds=5)


def test_saude(cliente_http):
    assert cliente_http.get("/saude").json() == {"status": "ok"}


def test_trabalhador_roda_os_vencidos_e_uma_falha_nao_para_os_outros(fabrica, politica):
    with fabrica() as sessao:
        primeira = sessao.scalars(select(Conversa)).one()
        segunda = Conversa(cobranca=primeira.cobranca)
        sessao.add(segunda)
        sessao.flush()
        agendar_turno(sessao, primeira.id, AGORA)
        agendar_turno(sessao, segunda.id, AGORA)
        sessao.commit()
        ids = [primeira.id, segunda.id]
    processados = []

    def processar(sessao, conversa_id, agora):
        processados.append(conversa_id)
        if conversa_id == ids[0]:
            raise RuntimeError("falha simulada")
        tomar_turno(sessao, conversa_id)

    trabalhador = Trabalhador(fabrica, processar, Relogio(AGORA + timedelta(seconds=1)))

    assert trabalhador.rodar_uma_vez() == 2
    assert processados == ids
    with fabrica() as sessao:
        # O que falhou continua na fila para o próximo ciclo.
        assert turnos_vencidos(sessao, AGORA + timedelta(seconds=1)) == [ids[0]]


def test_pendente_no_chatwoot_devolve_a_conversa_ao_robo(cliente_http, fabrica):
    _postar(cliente_http, _evento())
    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        conversa.human_owned = True
        sessao.commit()
    evento = {"event": "conversation_status_changed", "id": 555, "status": "pending"}

    assert _postar(cliente_http, evento).json() == {"resultado": "devolvida"}
    with fabrica() as sessao:
        assert sessao.scalars(select(Conversa)).one().human_owned is False


def test_outro_status_nao_devolve(cliente_http):
    _postar(cliente_http, _evento())
    evento = {"event": "conversation_status_changed", "id": 555, "status": "resolved"}
    assert _postar(cliente_http, evento).json() == {"resultado": "ignorado"}
