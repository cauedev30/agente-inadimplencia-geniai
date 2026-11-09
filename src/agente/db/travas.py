"""A ordem das travas de linha da cobrança, num só lugar.

Quem trava mais de uma destas linhas trava sempre na mesma ordem: as conversas da cobrança (por
id), a cobrança e, por último, o pagamento. É a ordem do turno ativo (`tomar_turno` segura a
conversa e só depois relê a cobrança) e da confirmação do pagamento (`pagamento/confirmar.py`); a
rotina das baixas externas e a conferência da cobrança a seguem, e dois lados nunca esperam um pelo
outro em ciclo. A trava é a de `FOR NO KEY UPDATE`, que não barra o `FOR KEY SHARE` das chaves
estrangeiras (a mensagem que a rotina de conclusão grava). O SQLite ignora as travas.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca, Conversa


def travar_conversas_e_cobranca(sessao: Session, cobranca_id: int) -> None:
    """Trava as conversas e depois a cobrança, e as relê já travadas.

    As conversas inteiras são relidas: o que outra transação gravou durante a espera (o handoff, o
    atendente que assumiu) vale para a decisão de quem chama.
    """
    sessao.scalars(
        select(Conversa)
        .where(Conversa.cobranca_id == cobranca_id)
        .order_by(Conversa.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).all()
    sessao.execute(
        select(Cobranca)
        .where(Cobranca.id == cobranca_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
