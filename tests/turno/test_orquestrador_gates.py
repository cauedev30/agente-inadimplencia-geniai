"""O orquestrador aplicando os gates (tarefa 3.6). O Jev está em test_orquestrador_sombra."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Conversa, Mensagem, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.tipos import EstadoConversa, Modo, MotivoHumano, OrigemMensagem, ResultadoTurno
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.fila import mensagens_pendentes
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)
DOMINGO = datetime(2026, 11, 1, 10, tzinfo=JP)


@pytest.fixture
def deps(politica):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=Modo.ATIVO,
        jev=JevFalso(),
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _cliente(sessao, conversa, momento=SEGUNDA_10H, texto="oi", mid="cw-1"):
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.CLIENTE, texto, mid, False, momento)


def _agente(sessao, conversa, quantas):
    for _ in range(quantas):
        registrar_mensagem(
            sessao, conversa.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
        )


def test_fora_da_janela_reagenda_e_guarda_as_mensagens(sessao, ligada, deps):
    _cliente(sessao, ligada, momento=DOMINGO)

    resultado = processar_turno(sessao, ligada.id, DOMINGO, deps)

    assert resultado is ResultadoTurno.ADIADO
    assert ligada.turno_em == datetime(2026, 11, 3, 8, tzinfo=JP)
    assert [m.texto for m in mensagens_pendentes(sessao, ligada.id)] == ["oi"]
    assert deps.chatwoot.chamadas == []


def test_no_teto_sai_a_6a_fixa_com_handoff(sessao, ligada, deps):
    _agente(sessao, ligada, deps.politica.teto_resolucao)
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.FIXA
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert ligada.motivo_humano is MotivoHumano.TETO
    assert mensagens_pendentes(sessao, ligada.id) == []


def test_depois_da_6a_o_cliente_fica_sem_resposta(sessao, ligada, deps):
    _agente(sessao, ligada, deps.politica.teto_resolucao)
    _cliente(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    _cliente(sessao, ligada, mid="cw-2")

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_em_sombra_nada_e_enviado(sessao, ligada, deps):
    deps.modo = Modo.SOMBRA
    _agente(sessao, ligada, deps.politica.teto_resolucao)
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    assert ligada.estado is EstadoConversa.INICIADO
    turno = sessao.scalars(select(Turno)).one()
    assert (turno.modo, turno.acao_principal) == (Modo.SOMBRA, "FIXA:CANAL_EXTERNO")


def test_caminho_livre_grava_o_turno_e_consome_a_rajada(sessao, ligada, deps):
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # O turno segue até o envio (o Jev falso sem regras reprova o texto: sai a resposta segura).
    assert resultado is ResultadoTurno.ENVIADO
    turno = sessao.scalars(select(Turno)).one()
    assert len(turno.mensagens_ids) == 1
    assert mensagens_pendentes(sessao, ligada.id) == []
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_humano_na_conversa_e_ignorado(sessao, ligada, deps):
    ligada.human_owned = True
    _cliente(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
    assert sessao.scalars(select(Turno)).one().resultado is ResultadoTurno.IGNORADO


def test_passou_das_24h_sai_o_template_de_retomada(sessao, ligada, deps):
    _cliente(sessao, ligada, momento=SEGUNDA_10H - timedelta(hours=25))

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.FIXA
    [envio] = deps.disparador.envios
    assert envio.template == deps.politica.template_retomada
    assert envio.variaveis == {"nome": "Cliente"}


def test_reinicio_abre_uma_conversa_nova(sessao, ligada, deps):
    telefone = ligada.cobranca.cliente.telefone
    deps.politica = deps.politica.model_copy(update={"telefones_equipe": frozenset({telefone})})
    _agente(sessao, ligada, 3)
    _cliente(sessao, ligada, texto=deps.politica.comando_reinicio)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    conversas = sessao.scalars(select(Conversa).order_by(Conversa.id)).all()
    assert [c.chatwoot_conversa_id for c in conversas] == [None, 555]
    nova = conversas[1]
    assert nova.cobranca_id == ligada.cobranca_id
    assert nova.estado is EstadoConversa.INICIADO
    # O histórico da conversa antiga continua gravado, com a confirmação do reinício no fim.
    assert len(sessao.scalars(select(Mensagem).where(Mensagem.conversa_id == ligada.id)).all()) == 5


def test_reinicio_depois_do_handoff_abre_uma_conversa_nova_com_o_robo(sessao, ligada, deps):
    telefone = ligada.cobranca.cliente.telefone
    deps.politica = deps.politica.model_copy(update={"telefones_equipe": frozenset({telefone})})
    ligada.human_owned = True
    ligada.estado = EstadoConversa.ATENDIMENTO_HUMANO
    _cliente(sessao, ligada, texto=deps.politica.comando_reinicio)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO

    antiga, nova = sessao.scalars(select(Conversa).order_by(Conversa.id)).all()
    assert antiga.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert antiga.chatwoot_conversa_id is None
    assert nova.chatwoot_conversa_id == 555
    assert nova.estado is EstadoConversa.INICIADO
    assert nova.human_owned is False
    # O reinício troca a conversa e confirma ao cliente (antes, ficava mudo).
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_reinicio]


def _equipe(deps, ligada):
    telefone = ligada.cobranca.cliente.telefone
    deps.politica = deps.politica.model_copy(update={"telefones_equipe": frozenset({telefone})})


def test_reinicio_confirma_e_a_confirmacao_fica_na_conversa_antiga(sessao, ligada, deps):
    _equipe(deps, ligada)
    _cliente(sessao, ligada, texto=deps.politica.comando_reinicio)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert deps.politica.texto_reinicio == "Conversa reiniciada."
    assert deps.chatwoot.enviadas(555) == ["Conversa reiniciada."]
    antiga, nova = sessao.scalars(select(Conversa).order_by(Conversa.id)).all()
    [confirmacao] = sessao.scalars(
        select(Mensagem).where(
            Mensagem.conversa_id == antiga.id, Mensagem.origem == OrigemMensagem.FIXA
        )
    )
    assert (confirmacao.texto, confirmacao.conta_no_teto) == ("Conversa reiniciada.", False)
    assert confirmacao.chatwoot_message_id is not None
    # O histórico da conversa nova começa limpo.
    assert sessao.scalars(select(Mensagem).where(Mensagem.conversa_id == nova.id)).all() == []


def test_a_mensagem_depois_do_comando_passa_para_a_conversa_nova_e_ganha_turno(
    sessao, ligada, deps
):
    _equipe(deps, ligada)
    _cliente(sessao, ligada, texto="oi", mid="cw-0")
    _cliente(sessao, ligada, texto=deps.politica.comando_reinicio, mid="cw-1")
    _cliente(sessao, ligada, texto="quanto eu devo?", mid="cw-2")
    _cliente(sessao, ligada, texto="e como pago?", mid="cw-3")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    antiga, nova = sessao.scalars(select(Conversa).order_by(Conversa.id)).all()
    assert [m.texto for m in mensagens_pendentes(sessao, nova.id)] == [
        "quanto eu devo?",
        "e como pago?",
    ]
    assert nova.turno_em == SEGUNDA_10H
    # A anterior ao comando continua descartada, e a antiga não fica com pendência.
    assert [m.texto for m in mensagens_pendentes(sessao, antiga.id)] == []
    restantes = sessao.scalars(
        select(Mensagem.texto).where(
            Mensagem.conversa_id == antiga.id, Mensagem.origem == OrigemMensagem.CLIENTE
        )
    ).all()
    assert list(restantes) == ["oi", deps.politica.comando_reinicio]


def test_o_turno_da_conversa_nova_responde_a_mensagem_que_veio_depois_do_comando(
    sessao, ligada, deps
):
    _equipe(deps, ligada)
    _cliente(sessao, ligada, texto=deps.politica.comando_reinicio, mid="cw-1")
    _cliente(sessao, ligada, texto="quanto eu devo?", mid="cw-2")
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    nova = sessao.scalars(select(Conversa).order_by(Conversa.id.desc())).first()

    resultado = processar_turno(sessao, nova.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert len(deps.chatwoot.enviadas(555)) == 2  # a confirmação e a resposta
