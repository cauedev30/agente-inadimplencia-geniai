"""O Pix que venceu sem pagamento (tarefa 6.6).

Vira expirado e pronto: não há novo contato com o cliente (G2.4). Se ele pedir de novo, o turno
gera um Pix novo.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Pagamento
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
