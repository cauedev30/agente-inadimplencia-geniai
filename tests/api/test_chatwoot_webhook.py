from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from agente.api import chatwoot_webhook
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


@pytest.mark.parametrize("guardado_sem_9", [False, True])
def test_o_contato_casa_com_e_sem_o_9(cliente_http, fabrica, guardado_sem_9):
    """A1: a carteira guarda o telefone com o 9 (como o CTN dá) e o Chatwoot pode mandar sem ele,
    e o contrário também."""
    sem_9 = TELEFONE[:5] + TELEFONE[6:]
    with fabrica() as sessao:
        cliente = sessao.scalars(select(Cliente)).one()
        cliente.telefone = sem_9 if guardado_sem_9 else TELEFONE
        sessao.commit()
    do_contato = TELEFONE if guardado_sem_9 else sem_9
    evento = _evento(sender={"type": "contact", "phone_number": do_contato})

    assert _postar(cliente_http, evento).json() == {"resultado": "agendado"}
    with fabrica() as sessao:
        assert sessao.scalars(select(Cliente)).one().telefone in (TELEFONE, sem_9)
        assert sessao.scalars(select(Conversa)).one().chatwoot_conversa_id == 555


def _outro_cliente_ativo(sessao, matricula, telefone):
    cliente = Cliente(matricula_ctn=matricula, nome="Outro Fictício", telefone=telefone)
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha="outubro",
    )
    sessao.add(Conversa(cobranca=cobranca))
    sessao.commit()


def _matricula_da_conversa_ligada(fabrica):
    with fabrica() as sessao:
        conversa = sessao.scalars(
            select(Conversa).where(Conversa.chatwoot_conversa_id == 555)
        ).one()
        return conversa.cobranca.cliente.matricula_ctn


def test_com_duas_cobrancas_casando_o_telefone_exato_vence(cliente_http, fabrica, caplog):
    """A1: o par com e sem o 9 casa nos dois; o contato é do que guarda o número como ele veio,
    mesmo com o id maior."""
    sem_9 = TELEFONE[:5] + TELEFONE[6:]
    with fabrica() as sessao:
        _outro_cliente_ativo(sessao, "F-2", sem_9)
    evento = _evento(sender={"type": "contact", "phone_number": sem_9})

    with caplog.at_level("WARNING"):
        assert _postar(cliente_http, evento).json() == {"resultado": "agendado"}
    assert _matricula_da_conversa_ligada(fabrica) == "F-2"
    assert not caplog.records


def test_sem_telefone_exato_a_cobranca_de_menor_id_vence_e_o_aviso_nao_leva_dado(
    cliente_http, fabrica, caplog
):
    sem_9 = TELEFONE[:5] + TELEFONE[6:]
    with fabrica() as sessao:
        sessao.scalars(select(Cliente)).one().telefone = sem_9
        _outro_cliente_ativo(sessao, "F-2", sem_9)
    evento = _evento(sender={"type": "contact", "phone_number": TELEFONE})

    with caplog.at_level("WARNING"):
        assert _postar(cliente_http, evento).json() == {"resultado": "agendado"}
    assert _matricula_da_conversa_ligada(fabrica) == "F-1"
    avisos = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(avisos) == 1
    assert "2" in avisos[0] and "[1, 2]" in avisos[0]
    for dado in (TELEFONE, sem_9, "Cliente Fictício", "Outro Fictício", "F-1", "F-2"):
        assert dado not in avisos[0]


def test_dois_exatos_tambem_escolhem_o_de_menor_id_e_avisam(cliente_http, fabrica, caplog):
    with fabrica() as sessao:
        _outro_cliente_ativo(sessao, "F-2", TELEFONE)
    evento = _evento(sender={"type": "contact", "phone_number": TELEFONE})

    with caplog.at_level("WARNING"):
        _postar(cliente_http, evento)
    assert _matricula_da_conversa_ligada(fabrica) == "F-1"
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_anexo_e_gravado_com_o_tipo_e_o_endereco(cliente_http, fabrica):
    anexo = {"file_type": "audio", "data_url": "https://chatwoot.invalid/a.ogg", "file_size": 4321}
    _postar(cliente_http, _evento(texto="", attachments=[anexo]))

    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).one().anexos == [
            {
                "tipo": "audio",
                "url": "https://chatwoot.invalid/a.ogg",
                "tamanho": 4321,
                "content_type": None,
            }
        ]


def test_o_content_type_do_anexo_e_gravado_quando_o_chatwoot_manda(cliente_http, fabrica):
    anexo = {"file_type": "audio", "data_url": "https://chatwoot.invalid/a.ogg"}
    _postar(cliente_http, _evento(texto="", attachments=[{**anexo, "content_type": "audio/ogg"}]))

    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).one().anexos[0]["content_type"] == "audio/ogg"


def test_anexo_sem_endereco_ainda_e_gravado(cliente_http, fabrica):
    _postar(cliente_http, _evento(texto="", attachments=[{"file_type": "audio"}]))

    with fabrica() as sessao:
        [anexo] = sessao.scalars(select(Mensagem)).one().anexos
    assert anexo["tipo"] == "audio" and anexo["url"] is None


def test_o_log_do_webhook_nao_leva_o_endereco_do_anexo(cliente_http, caplog):
    caplog.set_level("DEBUG")
    anexo = {"file_type": "audio", "data_url": "https://chatwoot.invalid/segredo-do-anexo.ogg"}

    _postar(cliente_http, _evento(texto="", attachments=[anexo]))

    assert "segredo-do-anexo" not in caplog.text


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


def _do_atendente(telefone=TELEFONE, mensagem_id=200, texto="Seu débito é de R$ 100,20", **extra):
    """O `outgoing` do atendente como o webhook o traz: o contato vai em `conversation.meta`."""
    contato = {"phone_number": telefone} if telefone else {}
    return _evento(
        mensagem_id=mensagem_id,
        texto=texto,
        message_type="outgoing",
        sender={"type": "user", "name": "Atendente Inventada"},
        conversation={"id": 555, "meta": {"sender": contato}},
        **extra,
    )


def test_atendente_antes_do_cliente_grava_a_mensagem_e_nao_agenda_turno(cliente_http, fabrica):
    assert _postar(cliente_http, _do_atendente()).json() == {"resultado": "humano"}

    with fabrica() as sessao:
        conversa = sessao.scalars(select(Conversa)).one()
        assert conversa.chatwoot_conversa_id == 555
        assert conversa.human_owned is True
        # Só o cliente agenda o turno.
        assert conversa.turno_em is None
        mensagem = sessao.scalars(select(Mensagem)).one()
        assert (mensagem.origem, mensagem.texto) == (
            OrigemMensagem.HUMANO,
            "Seu débito é de R$ 100,20",
        )


def test_a_resposta_do_cliente_depois_do_atendente_entra_na_mesma_conversa(cliente_http, fabrica):
    _postar(cliente_http, _do_atendente())

    assert _postar(cliente_http, _evento(mensagem_id=101, texto="já paguei")).json() == {
        "resultado": "agendado"
    }
    with fabrica() as sessao:
        assert len(sessao.scalars(select(Conversa)).all()) == 1
        origens = [m.origem for m in sessao.scalars(select(Mensagem).order_by(Mensagem.id))]
        assert origens == [OrigemMensagem.HUMANO, OrigemMensagem.CLIENTE]


def test_atendente_de_telefone_fora_da_carteira_e_sem_cobranca(cliente_http, fabrica):
    evento = _do_atendente(telefone="+5583900000000")

    assert _postar(cliente_http, evento).json() == {"resultado": "sem_cobranca"}
    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).all() == []
        assert sessao.scalars(select(Conversa)).one().chatwoot_conversa_id is None


def test_atendente_sem_telefone_no_evento_e_sem_cobranca(cliente_http, fabrica):
    assert _postar(cliente_http, _do_atendente(telefone=None)).json() == {
        "resultado": "sem_cobranca"
    }


def test_a_mesma_mensagem_do_atendente_duas_vezes_grava_uma_so(cliente_http, fabrica):
    _postar(cliente_http, _do_atendente())
    _postar(cliente_http, _do_atendente())

    with fabrica() as sessao:
        assert len(sessao.scalars(select(Mensagem)).all()) == 1
        assert len(sessao.scalars(select(Conversa)).all()) == 1


def test_robo_e_nota_privada_antes_do_cliente_continuam_ignorados(cliente_http, fabrica):
    do_robo = _do_atendente(mensagem_id=210)
    do_robo["sender"] = {"type": "agent_bot"}
    nota = _do_atendente(mensagem_id=211, private=True)

    assert _postar(cliente_http, do_robo).json() == {"resultado": "ignorado"}
    assert _postar(cliente_http, nota).json() == {"resultado": "ignorado"}
    with fabrica() as sessao:
        assert sessao.scalars(select(Mensagem)).all() == []
        assert sessao.scalars(select(Conversa)).one().human_owned is False


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


@pytest.mark.parametrize("tipo", ["outgoing", "incoming"])
def test_a_nota_privada_da_transcricao_nao_vira_mensagem_do_cliente(cliente_http, fabrica, tipo):
    _postar(cliente_http, _evento())
    nota = _evento(
        mensagem_id=302,
        texto="Transcrição do áudio (agente): já paguei",
        message_type=tipo,
        private=True,
        sender={"type": "agent_bot"},
    )

    assert _postar(cliente_http, nota).json() == {"resultado": "ignorado"}

    with fabrica() as sessao:
        textos = [m.texto for m in sessao.scalars(select(Mensagem))]
    assert textos == ["quanto eu devo?"]


def _outra_entrega_cria_a_conversa(monkeypatch, sessao):
    """Entre o `select` e o `flush` da nossa entrega, a outra (bot e webhook) grava a conversa."""
    normalizar = chatwoot_webhook.variantes_telefone

    def com_a_corrida(telefone):
        cobranca = sessao.scalars(select(Cobranca)).one()
        sessao.execute(insert(Conversa).values(cobranca_id=cobranca.id, chatwoot_conversa_id=555))
        return normalizar(telefone)

    monkeypatch.setattr(chatwoot_webhook, "variantes_telefone", com_a_corrida)


def test_a_corrida_na_primeira_mensagem_usa_a_conversa_criada_pela_outra_entrega(
    fabrica, politica, monkeypatch
):
    config = Config(database_url="sqlite://", chatwoot_conta=7, chatwoot_inbox=3)
    with fabrica() as sessao:
        _outra_entrega_cria_a_conversa(monkeypatch, sessao)

        resultado = chatwoot_webhook.receber_evento(sessao, _evento(), politica, config, AGORA)

        assert resultado == "agendado"
        existente = sessao.scalars(
            select(Conversa).where(Conversa.chatwoot_conversa_id == 555)
        ).one()
        assert [m.conversa_id for m in sessao.scalars(select(Mensagem)).all()] == [existente.id]
        assert existente.turno_em == AGORA + timedelta(seconds=politica.rajada_segundos)


def test_a_corrida_tambem_vale_para_a_mensagem_do_atendente(fabrica, politica, monkeypatch):
    config = Config(database_url="sqlite://", chatwoot_conta=7, chatwoot_inbox=3)
    with fabrica() as sessao:
        _outra_entrega_cria_a_conversa(monkeypatch, sessao)

        resultado = chatwoot_webhook.receber_evento(
            sessao, _do_atendente(), politica, config, AGORA
        )

        assert resultado == "humano"
        mensagem = sessao.scalars(select(Mensagem)).one()
        assert mensagem.origem == OrigemMensagem.HUMANO
        existente = sessao.scalars(
            select(Conversa).where(Conversa.chatwoot_conversa_id == 555)
        ).one()
        assert (mensagem.conversa_id, existente.human_owned) == (existente.id, True)


def test_a_corrida_sem_conversa_livre_nao_deixa_conversa_sobrando(fabrica, politica, monkeypatch):
    """Sem conversa livre, a entrega cria uma nova; ao perder a corrida, ela não pode sobrar."""
    config = Config(database_url="sqlite://", chatwoot_conta=7, chatwoot_inbox=3)
    with fabrica() as sessao:
        sessao.scalars(select(Conversa)).one().chatwoot_conversa_id = 444
        sessao.commit()
        _outra_entrega_cria_a_conversa(monkeypatch, sessao)

        resultado = chatwoot_webhook.receber_evento(sessao, _evento(), politica, config, AGORA)

        assert resultado == "agendado"
        assert sorted(c.chatwoot_conversa_id for c in sessao.scalars(select(Conversa))) == [
            444,
            555,
        ]
