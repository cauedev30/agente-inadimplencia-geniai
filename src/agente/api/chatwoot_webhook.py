"""Webhook do Agent Bot do Chatwoot: grava a mensagem e agenda o turno (tarefa 3.4).

O evento segue o webhook do Chatwoot (`message_created`). A conta e a inbox reais são da T3.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.config import Config
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.telefone import normalizar_telefone, variantes_telefone
from agente.integracoes.ctn_teste import CTNDeCasos
from agente.politica import Politica
from agente.teste.casos import garantir_caso
from agente.tipos import OrigemMensagem, StatusCobranca
from agente.turno.fila import agendar_turno
from agente.turno.handoff import devolver_ao_robo

log = logging.getLogger(__name__)


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


def _anexo(bruto: dict) -> dict:
    """O que se guarda do anexo: o tipo e, para baixar depois (o áudio, D43), o endereço, o tamanho
    e o content-type que o Chatwoot mandar. O endereço nunca vai ao log."""
    return {
        "tipo": bruto.get("file_type"),
        "url": bruto.get("data_url"),
        "tamanho": bruto.get("file_size"),
        "content_type": bruto.get("content_type"),
    }


def ler_evento(dados: dict) -> EventoMensagem | None:
    """Lê um `message_created`. Outros eventos devolvem None."""
    if dados.get("event") != "message_created":
        return None
    conversa = dados.get("conversation") or {}
    if conversa.get("id") is None or dados.get("id") is None:
        # Sem a conversa ou a mensagem não há o que tratar: vale como evento ignorado (AUD-34).
        return None
    remetente = dados.get("sender") or {}
    # No `outgoing` o `sender` é o atendente: o telefone do cliente só vem no contato da conversa
    # (`conversation.meta.sender`). Na mensagem do cliente, o próprio `sender` vale mais.
    contato = ((conversa.get("meta") or {}).get("sender")) or {}
    if dados.get("message_type") == "outgoing":
        telefone = contato.get("phone_number")
    else:
        telefone = remetente.get("phone_number") or contato.get("phone_number")
    texto = dados.get("content") or ""
    # O toque num botão de resposta do template chega com o texto do botão; se o Chatwoot
    # entregar o payload, ele vale mais (a confirmar na T3, com o Chatwoot real).
    payload = (dados.get("content_attributes") or {}).get("payload")
    if payload == BOTAO_PAGAR_PIX.valor:
        texto = BOTAO_PAGAR_PIX.texto
    return EventoMensagem(
        mensagem_id=str(dados["id"]),
        conversa_chatwoot_id=int(conversa["id"]),
        conta_id=(dados.get("account") or {}).get("id"),
        inbox_id=(dados.get("inbox") or {}).get("id"),
        tipo=dados.get("message_type", ""),
        privada=bool(dados.get("private")),
        remetente=remetente.get("type", ""),
        telefone=telefone,
        texto=texto,
        anexos=[_anexo(a) for a in dados.get("attachments") or []],
    )


def _de_outro_lugar(evento: EventoMensagem, config: Config) -> bool:
    if config.chatwoot_conta is not None and evento.conta_id != config.chatwoot_conta:
        return True
    return config.chatwoot_inbox is not None and evento.inbox_id != config.chatwoot_inbox


def _conversa_ligada(sessao: Session, evento: EventoMensagem) -> Conversa | None:
    return sessao.scalar(
        select(Conversa).where(Conversa.chatwoot_conversa_id == evento.conversa_chatwoot_id)
    )


def _cobranca_ativa_do_telefone(
    sessao: Session, bruto: str, variantes: frozenset[str]
) -> Cobranca | None:
    """A cobrança ativa dona do telefone. Com e sem o 9 podem ser duas pessoas: vale a que guarda o
    número exatamente como o contato mandou e, se ainda sobrar mais de uma, a de menor id (sempre a
    mesma). O aviso leva só a contagem e os ids, nunca telefone nem nome."""
    candidatas = sessao.scalars(
        select(Cobranca)
        .join(Cliente)
        .where(Cliente.telefone.in_(variantes), Cobranca.status == StatusCobranca.ATIVA)
        .order_by(Cobranca.id)
    ).all()
    if not candidatas:
        return None
    exato = normalizar_telefone(bruto)
    restantes = [c for c in candidatas if c.cliente.telefone == exato] or candidatas
    if len(restantes) > 1:
        log.warning(
            "%s cobranças ativas casam com o telefone do contato (ids %s); vale a de menor id",
            len(restantes),
            [c.id for c in restantes],
        )
    return restantes[0]


def _conversa_do_cliente(sessao: Session, evento: EventoMensagem) -> Conversa | None:
    """A conversa já ligada ao Chatwoot ou, na 1ª mensagem (a do cliente ou a do atendente que abriu
    a cobrança), a da cobrança ativa do telefone.

    O bot e o webhook entregam a mesma 1ª mensagem ao mesmo tempo: se a outra entrega ligar a
    conversa antes, a ligação daqui quebra a unicidade. Ela roda num savepoint e, nesse caso,
    usa a conversa que a outra criou (A8 do ensaio de 2026-11-04)."""
    conversa = _conversa_ligada(sessao, evento)
    if conversa is not None:
        return conversa
    # O contato pode vir com ou sem o 9 do celular, e a carteira guarda como o CTN deu (A1).
    telefones = variantes_telefone(evento.telefone or "")
    if not telefones:
        return None
    cobranca = _cobranca_ativa_do_telefone(sessao, evento.telefone or "", telefones)
    if cobranca is None:
        return None
    sem_chatwoot = [c for c in cobranca.conversas if c.chatwoot_conversa_id is None]
    conversa = sem_chatwoot[-1] if sem_chatwoot else Conversa(cobranca_id=cobranca.id)
    try:
        with sessao.begin_nested():
            conversa.chatwoot_conversa_id = evento.conversa_chatwoot_id
            sessao.add(conversa)
    except IntegrityError:
        existente = _conversa_ligada(sessao, evento)
        if existente is None:
            raise
        return existente
    return conversa


def _conversa_do_1o_contato_de_teste(
    sessao: Session,
    evento: EventoMensagem,
    casos: CTNDeCasos,
    politica: Politica,
    agora: datetime,
) -> Conversa | None:
    """O 1º contato de um testador do piloto: o caso padrão nasce e a conversa se liga a ele.

    Se a outra entrega da mesma 1ª mensagem criou o caso antes (o índice único de cobrança ATIVA
    ou a matrícula repetida quebra a nossa), a criação daqui roda num savepoint e vale a dela."""
    try:
        with sessao.begin_nested():
            if garantir_caso(sessao, casos, politica, evento.telefone or "", agora) is None:
                return None
    except IntegrityError:
        pass
    return _conversa_do_cliente(sessao, evento)


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
    sessao: Session,
    dados: dict,
    politica: Politica,
    config: Config,
    agora: datetime,
    casos: CTNDeCasos | None = None,
) -> str:
    """Trata o evento. Devolve: agendado, repetida, humano, devolvida, sem_cobranca ou ignorado.

    A mensagem do atendente só grava e marca o humano na conversa; quem agenda o turno é o cliente.
    Com `casos` (o modo teste, D56), o 1º contato de um testador do piloto, sem cobrança ATIVA,
    ganha o caso padrão do cardápio.
    """
    if dados.get("event") == "conversation_status_changed":
        return _status_mudou(sessao, dados, agora)
    evento = ler_evento(dados)
    if evento is None or _de_outro_lugar(evento, config) or evento.privada:
        return "ignorado"

    if evento.tipo == "outgoing":
        # Só a mensagem do atendente interessa: a do próprio robô já foi gravada no envio.
        if evento.remetente != "user":
            return "ignorado"
        # Na JP Sul quem abre a cobrança é o atendente, antes de o cliente responder: a conversa
        # nasce aqui, para o Jev ler o turno sabendo que a dívida já foi informada.
        conversa = _conversa_do_cliente(sessao, evento)
        if conversa is None:
            sessao.commit()
            return "sem_cobranca"
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
    if conversa is None and casos is not None and evento.telefone:
        conversa = _conversa_do_1o_contato_de_teste(sessao, evento, casos, politica, agora)
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
