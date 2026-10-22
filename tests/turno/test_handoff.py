from datetime import UTC, datetime

from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, transicionar
from agente.db.modelos import Mensagem
from agente.integracoes.chatwoot import ChatwootFalso
from agente.tipos import EstadoConversa, Laudo, MotivoHumano, OrigemMensagem
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.handoff import devolver_ao_robo, fazer_handoff

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
LAUDOS = [Laudo("risco_juridico", True, 0.91, 0.80)]


def _handoff(sessao, conversa, politica, chatwoot, time_id=4):
    conversa.chatwoot_conversa_id = 555
    fazer_handoff(
        sessao, chatwoot, conversa, MotivoHumano.JURIDICO, LAUDOS, politica, time_id, AGORA
    )


def test_handoff_faz_as_seis_etapas_na_ordem(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff(sessao, conversa, politica, chatwoot)

    assert [chamada[0] for chamada in chatwoot.chamadas] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
        "enviar_mensagem",
    ]
    nota = chatwoot.chamadas[0][2]
    assert "JURIDICO" in nota and "risco_juridico" in nota and "0.91" in nota
    assert chatwoot.chamadas[1][2] == ["juridico"]
    assert chatwoot.chamadas[2][2] == 4
    assert chatwoot.chamadas[3][2] == "open"
    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert conversa.motivo_humano is MotivoHumano.JURIDICO


def test_mensagem_do_handoff_e_fixa_e_fora_do_teto(sessao, conversa, politica):
    _handoff(sessao, conversa, politica, ChatwootFalso())

    mensagem = sessao.scalars(select(Mensagem)).one()
    assert mensagem.origem is OrigemMensagem.FIXA
    assert mensagem.chatwoot_message_id == "falso-5"
    assert mensagens_no_teto(sessao, conversa.id) == 0


def test_sem_time_configurado_nao_atribui(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff(sessao, conversa, politica, chatwoot, time_id=None)

    assert "atribuir" not in [chamada[0] for chamada in chatwoot.chamadas]


def test_pendente_devolve_ao_robo_no_estado_de_antes(sessao, conversa, politica):
    transicionar(sessao, conversa, EstadoConversa.EM_NEGOCIACAO, None, None, momento=AGORA)
    _handoff(sessao, conversa, politica, ChatwootFalso())
    conversa.human_owned = True

    devolver_ao_robo(sessao, conversa, AGORA)

    assert conversa.estado is EstadoConversa.EM_NEGOCIACAO
    assert conversa.human_owned is False
    assert conversa.motivo_humano is None
