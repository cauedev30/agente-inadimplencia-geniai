"""O aviso ao atendente quando o link do webchat bloqueia por erros no CPF (tarefa 7.2)."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Conversa
from agente.integracoes.chatwoot import Chatwoot

log = logging.getLogger(__name__)

ETIQUETA_BLOQUEADO = "webchat_bloqueado"


def avisar_bloqueio(
    sessao: Session, chatwoot: Chatwoot, cobranca_id: int, horas: int, tentativas: int
) -> None:
    """Etiqueta e deixa a nota na conversa do WhatsApp da cobrança. O CPF digitado não vai junto."""
    conversa = sessao.scalar(
        select(Conversa)
        .where(Conversa.cobranca_id == cobranca_id, Conversa.chatwoot_conversa_id.is_not(None))
        .order_by(Conversa.id.desc())
    )
    if conversa is None:
        log.warning("webchat: bloqueio da cobrança %s sem conversa no Chatwoot", cobranca_id)
        return
    chatwoot.etiquetar(conversa.chatwoot_conversa_id, [ETIQUETA_BLOQUEADO])
    chatwoot.nota_privada(
        conversa.chatwoot_conversa_id,
        f"O link do webchat foi bloqueado por {horas}h depois de {tentativas} erros nos dígitos do "
        "CPF. Pode ter sido o próprio cliente ou quem recebeu o link. Se for o cliente, atenda "
        "por aqui; as tentativas voltam sozinhas depois do prazo.",
    )
