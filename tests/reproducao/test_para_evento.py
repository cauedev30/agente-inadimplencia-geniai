"""O adaptador da mensagem da API para o `message_created` do webhook (tarefa 10.2)."""

from datetime import UTC, datetime

from sqlalchemy import select

from agente.api.chatwoot_webhook import EventoMensagem, ler_evento, receber_evento
from agente.config import Config
from agente.db.modelos import Mensagem
from agente.reproducao.chatwoot_leitura import ConversaLida, conversa_lida, para_evento
from agente.tipos import OrigemMensagem

from .fabrica import CONTA, INBOX, SEGUNDA_10H, TELEFONE, anexo, mensagem
from .fabrica import conversa as fabrica_conversa

AGORA = datetime.fromtimestamp(SEGUNDA_10H, UTC)
LIDA = conversa_lida(fabrica_conversa(555, SEGUNDA_10H))


def test_conversa_lida_le_o_telefone_do_meta():
    assert LIDA.id == 555
    assert LIDA.telefone == TELEFONE
    assert (LIDA.conta_id, LIDA.inbox_id) == (CONTA, INBOX)


def test_incoming_vira_a_mensagem_do_contato_com_o_telefone_da_conversa():
    evento = ler_evento(para_evento(mensagem(10, 555, 0, "quanto eu devo?", SEGUNDA_10H), LIDA))

    assert evento == EventoMensagem(
        mensagem_id="10",
        conversa_chatwoot_id=555,
        conta_id=CONTA,
        inbox_id=INBOX,
        tipo="incoming",
        privada=False,
        remetente="contact",
        telefone=TELEFONE,
        texto="quanto eu devo?",
        anexos=[],
    )


def test_o_telefone_da_conversa_vale_mais_que_o_da_mensagem():
    bruta = mensagem(10, 555, 0, "oi", SEGUNDA_10H)
    bruta["sender"]["phone_number"] = "+5583999990099"

    assert ler_evento(para_evento(bruta, LIDA)).telefone == TELEFONE


def test_sem_telefone_na_conversa_usa_o_da_mensagem():
    bruta = mensagem(10, 555, 0, "oi", SEGUNDA_10H)
    bruta["sender"]["phone_number"] = "+5583999990099"
    sem_telefone = ConversaLida(555, CONTA, INBOX, None, AGORA, AGORA)

    assert ler_evento(para_evento(bruta, sem_telefone)).telefone == "+5583999990099"


def test_outgoing_do_atendente_vira_mensagem_de_user_sem_telefone():
    evento = ler_evento(para_evento(mensagem(11, 555, 1, "Bom dia", SEGUNDA_10H), LIDA))

    assert (evento.tipo, evento.remetente, evento.telefone) == ("outgoing", "user", None)
    assert evento.texto == "Bom dia"


def test_outgoing_de_template_nao_e_do_atendente():
    # O tipo 3 é o template (a abertura): chega como "template", e o webhook não o trata.
    evento = ler_evento(para_evento(mensagem(12, 555, 3, "[template]", SEGUNDA_10H), LIDA))

    assert evento.tipo == "template"


def test_o_tipo_do_remetente_vem_do_sender_type_e_nao_do_topo():
    bruta = mensagem(11, 555, 1, "Oi", SEGUNDA_10H, remetente="agent_bot")
    bruta["sender_type"] = ""

    assert ler_evento(para_evento(bruta, LIDA)).remetente == "agent_bot"


def test_atividade_e_ignorada():
    assert para_evento(mensagem(13, 555, 2, "Conversa aberta", SEGUNDA_10H), LIDA) is None


def test_nota_privada_e_ignorada():
    assert para_evento(mensagem(14, 555, 1, "nota", SEGUNDA_10H, privada=True), LIDA) is None


def test_tipo_desconhecido_e_ignorado():
    assert para_evento(mensagem(15, 555, 9, "?", SEGUNDA_10H), LIDA) is None


def test_audio_chega_com_o_anexo():
    bruta = mensagem(16, 555, 0, "", SEGUNDA_10H, anexos=[anexo("audio", 16)])

    evento = ler_evento(para_evento(bruta, LIDA))

    assert evento.texto == ""
    assert [a["tipo"] for a in evento.anexos] == ["audio"]
    assert evento.anexos[0]["url"] == "https://chatwoot.invalid/arquivo-inventado"
    assert evento.anexos[0]["tamanho"] == 1234


def test_mensagem_sem_texto_vira_texto_vazio():
    bruta = mensagem(17, 555, 0, "", SEGUNDA_10H)
    bruta["content"] = None

    assert ler_evento(para_evento(bruta, LIDA)).texto == ""


def test_as_mensagens_de_uma_conversa_viram_os_registros_do_webhook(sessao, conversa, politica):
    # O cliente da fixture tem outro telefone: a conversa do Chatwoot chega com o dele.
    lida = conversa_lida(fabrica_conversa(555, SEGUNDA_10H, telefone="+5583900000001"))
    config = Config(database_url="sqlite://", chatwoot_conta=CONTA, chatwoot_inbox=INBOX)
    mensagens = [
        mensagem(1, 555, 3, "[template]", SEGUNDA_10H),
        mensagem(2, 555, 2, "Conversa aberta", SEGUNDA_10H + 1),
        mensagem(3, 555, 0, "quanto eu devo?", SEGUNDA_10H + 2),
        mensagem(4, 555, 1, "nota interna", SEGUNDA_10H + 3, privada=True),
        mensagem(5, 555, 1, "Já te respondo", SEGUNDA_10H + 4),
        mensagem(6, 555, 0, "", SEGUNDA_10H + 5, anexos=[anexo("audio", 6)]),
    ]

    resultados = []
    for bruta in mensagens:
        dados = para_evento(bruta, lida)
        resultados.append(
            "ignorado" if dados is None else receber_evento(sessao, dados, politica, config, AGORA)
        )

    assert resultados == ["ignorado", "ignorado", "agendado", "ignorado", "humano", "agendado"]
    gravadas = sessao.scalars(select(Mensagem).order_by(Mensagem.id)).all()
    assert [(m.origem, m.texto, m.chatwoot_message_id) for m in gravadas] == [
        (OrigemMensagem.CLIENTE, "quanto eu devo?", "3"),
        (OrigemMensagem.HUMANO, "Já te respondo", "5"),
        (OrigemMensagem.CLIENTE, "", "6"),
    ]
    assert gravadas[2].anexos[0]["tipo"] == "audio"
    assert gravadas[0].conversa.human_owned is True
