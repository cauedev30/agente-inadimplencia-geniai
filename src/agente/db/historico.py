"""Grava o histórico da conversa: mensagens, turnos, laudos do Jev e transições de estado.

As funções só fazem flush; quem chama decide o commit, para o turno ser gravado de uma vez.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agente.db.modelos import Conversa, LaudoGravado, Mensagem, Transicao, Turno
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Laudo,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)


def registrar_mensagem(
    sessao: Session,
    conversa_id: int,
    origem: OrigemMensagem,
    texto: str,
    chatwoot_message_id: str | None,
    conta_no_teto: bool,
    momento: datetime,
) -> Mensagem | None:
    """Grava a mensagem. Devolve None quando o chatwoot_message_id já existe (dedupe)."""
    mensagem = Mensagem(
        conversa_id=conversa_id,
        origem=origem,
        texto=texto,
        chatwoot_message_id=chatwoot_message_id,
        conta_no_teto=conta_no_teto,
        momento=momento,
    )
    try:
        with sessao.begin_nested():
            sessao.add(mensagem)
    except IntegrityError:
        return None
    return mensagem


def mensagens_no_teto(sessao: Session, conversa_id: int) -> int:
    consulta = select(func.count(Mensagem.id)).where(
        Mensagem.conversa_id == conversa_id, Mensagem.conta_no_teto.is_(True)
    )
    return sessao.scalar(consulta) or 0


def abrir_turno(sessao: Session, conversa_id: int, mensagens_ids: list[int], modo: Modo) -> Turno:
    turno = Turno(conversa_id=conversa_id, mensagens_ids=list(mensagens_ids), modo=modo)
    sessao.add(turno)
    sessao.flush()
    return turno


def gravar_laudos(
    sessao: Session, turno_id: int, batch: BatchJev, laudos: list[Laudo]
) -> list[int]:
    gravados = [
        LaudoGravado(
            turno_id=turno_id,
            batch=batch,
            pergunta=laudo.pergunta,
            valor=laudo.valor,
            probabilidade=laudo.probabilidade,
            confianca=laudo.confianca,
        )
        for laudo in laudos
    ]
    sessao.add_all(gravados)
    sessao.flush()
    return [gravado.id for gravado in gravados]


def fechar_turno(
    sessao: Session,
    turno_id: int,
    resultado: ResultadoTurno,
    *,
    acao_principal: str | None = None,
    acoes_anexadas: list[str] | None = None,
    numeros: dict | None = None,
    texto_enviado: str | None = None,
    momento: datetime | None = None,
) -> None:
    turno = sessao.get_one(Turno, turno_id)
    turno.resultado = resultado
    turno.acao_principal = acao_principal
    turno.acoes_anexadas = list(acoes_anexadas or [])
    turno.numeros = dict(numeros or {})
    turno.texto_enviado = texto_enviado
    turno.fechado_em = momento or datetime.now(UTC)
    sessao.flush()


def transicionar(
    sessao: Session,
    conversa: Conversa,
    para: EstadoConversa,
    motivo: MotivoHumano | None,
    laudo_id: int | None,
    *,
    momento: datetime | None = None,
) -> None:
    """Muda o estado e grava a transição com o laudo que a causou (auditoria)."""
    sessao.add(
        Transicao(
            conversa_id=conversa.id,
            de=conversa.estado,
            para=para,
            motivo=motivo,
            laudo_id=laudo_id,
            momento=momento or datetime.now(UTC),
        )
    )
    conversa.estado = para
    conversa.motivo_humano = motivo if para is EstadoConversa.ATENDIMENTO_HUMANO else None
    sessao.flush()
