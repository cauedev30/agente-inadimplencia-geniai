"""Webhook do Agent Bot do Chatwoot: grava a mensagem e agenda o turno (tarefa 3.4).

O evento segue o webhook do Chatwoot (`message_created`). A conta e a inbox reais são da T3.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.carteira.importar import normalizar_telefone
from agente.config import Config
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.politica import Politica
from agente.tipos import OrigemMensagem, StatusCobranca
from agente.turno.fila import agendar_turno
from agente.turno.handoff import devolver_ao_robo


@dataclass(frozen=True)
class EventoMensagem:
    mensagem_id: str
    conversa_chatwoot_id: int
    conta_id: int | None
    inbox_id: int | None
    tipo: str  # incoming ou outgoing
    privada: bool
    remetente: str  # contact, user ou agent_bot
    telefone: str | None
    texto: str
    anexos: list[dict]


def ler_evento(dados: dict) -> EventoMensagem | None:
    """Lê um `message_created`. Outros eventos devolvem None."""
    if dados.get("event") != "message_created":
        return None
    remetente = dados.get("sender") or {}
    return EventoMensagem(
        mensagem_id=str(dados["id"]),
        conversa_chatwoot_id=int(dados["conversation"]["id"]),
        conta_id=(dados.get("account") or {}).get("id"),
        inbox_id=(dados.get("inbox") or {}).get("id"),
        tipo=dados.get("message_type", ""),
        privada=bool(dados.get("private")),
        remetente=remetente.get("type", ""),
        telefone=remetente.get("phone_number"),
        texto=dados.get("content") or "",
        anexos=[{"tipo": a.get("file_type")} for a in dados.get("attachments") or []],
    )


def _de_outro_lugar(evento: EventoMensagem, config: Config) -> bool:
    if config.chatwoot_conta is not None and evento.conta_id != config.chatwoot_conta:
        return True
    return config.chatwoot_inbox is not None and evento.inbox_id != config.chatwoot_inbox


def _conversa_do_cliente(sessao: Session, evento: EventoMensagem) -> Conversa | None:
    """A conversa já ligada ao Chatwoot ou, na 1ª resposta, a da cobrança ativa do telefone."""
    conversa = sessao.scalar(
        select(Conversa).where(Conversa.chatwoot_conversa_id == evento.conversa_chatwoot_id)
    )
    if conversa is not None:
        return conversa
    telefone = normalizar_telefone(evento.telefone or "")
    if telefone is None:
        return None
    cobranca = sessao.scalar(
        select(Cobranca)
        .join(Cliente)
        .where(Cliente.telefone == telefone, Cobranca.status == StatusCobranca.ATIVA)
    )
    if cobranca is None:
        return None
    sem_chatwoot = [c for c in cobranca.conversas if c.chatwoot_conversa_id is None]
    conversa = sem_chatwoot[-1] if sem_chatwoot else Conversa(cobranca=cobranca)
    conversa.chatwoot_conversa_id = evento.conversa_chatwoot_id
    sessao.add(conversa)
    sessao.flush()
    return conversa


def _status_mudou(sessao: Session, dados: dict, agora: datetime) -> str:
    """O atendente marcou a conversa como Pendente: o robô reassume (arquitetura, seção 3)."""
    if dados.get("status") != "pending":
        return "ignorado"
    conversa = sessao.scalar(
        select(Conversa).where(Conversa.chatwoot_conversa_id == int(dados.get("id", 0)))
    )
    if conversa is None:
        return "ignorado"
    devolver_ao_robo(sessao, conversa, agora)
    sessao.commit()
    return "devolvida"


def receber_evento(
    sessao: Session, dados: dict, politica: Politica, config: Config, agora: datetime
) -> str:
    """Trata o evento. Devolve: agendado, repetida, humano, devolvida, sem_cobranca ou ignorado."""
    if dados.get("event") == "conversation_status_changed":
        return _status_mudou(sessao, dados, agora)
    evento = ler_evento(dados)
    if evento is None or _de_outro_lugar(evento, config) or evento.privada:
        return "ignorado"

    if evento.tipo == "outgoing":
        # Só a mensagem do atendente interessa: a do próprio robô já foi gravada no envio.
        if evento.remetente != "user":
            return "ignorado"
        conversa = sessao.scalar(
            select(Conversa).where(Conversa.chatwoot_conversa_id == evento.conversa_chatwoot_id)
        )
        if conversa is None:
            return "ignorado"
        conversa.human_owned = True
        registrar_mensagem(
            sessao,
            conversa.id,
            OrigemMensagem.HUMANO,
            evento.texto,
            evento.mensagem_id,
            False,
            agora,
        )
        sessao.commit()
        return "humano"

    if evento.tipo != "incoming":
        return "ignorado"
    conversa = _conversa_do_cliente(sessao, evento)
    if conversa is None:
        sessao.commit()
        return "sem_cobranca"
    mensagem = registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.CLIENTE,
        evento.texto,
        evento.mensagem_id,
        conta_no_teto=False,
        momento=agora,
        anexos=evento.anexos,
    )
    if mensagem is None:
        sessao.commit()
        return "repetida"
    agendar_turno(sessao, conversa.id, agora + timedelta(seconds=politica.rajada_segundos))
    sessao.commit()
    return "agendado"
