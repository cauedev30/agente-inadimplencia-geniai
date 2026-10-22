"""O Pix que venceu sem pagamento (tarefa 6.6).

Vira expirado e pronto: não há novo contato com o cliente (G2.4). Se ele pedir de novo, o turno
gera um Pix novo.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca, Pagamento
from agente.tipos import FormaPagamento, StatusPagamento


def expirar_pix(sessao: Session, agora: datetime) -> int:
    """Marca como expirados os Pix pendentes com o prazo vencido. Devolve quantos foram."""
    vencidos = sessao.scalars(
        select(Pagamento).where(
            Pagamento.forma == FormaPagamento.PIX,
            Pagamento.status == StatusPagamento.PENDENTE,
            Pagamento.expira_em.is_not(None),
            Pagamento.expira_em < agora,
        )
    ).all()
    for pagamento in vencidos:
        pagamento.status = StatusPagamento.EXPIRADO
    sessao.flush()
    return len(vencidos)


def expirar_pendentes(cobranca: Cobranca) -> int:
    """Expira os Pix pendentes da cobrança, de uma vez (o handoff com `handoff_expira_pix`)."""
    expirados = 0
    for pagamento in cobranca.pagamentos:
        if pagamento.forma is FormaPagamento.PIX and pagamento.status is StatusPagamento.PENDENTE:
            pagamento.status = StatusPagamento.EXPIRADO
            expirados += 1
    return expirados
