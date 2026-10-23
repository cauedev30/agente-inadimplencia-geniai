"""as tentativas de CPF e a conversa do webchat da cobrança

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("webchat_tentativas_cpf", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("webchat_bloqueado_ate", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("webchat_verificado_em", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("webchat_conversa_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.drop_column("webchat_conversa_id")
        batch_op.drop_column("webchat_verificado_em")
        batch_op.drop_column("webchat_bloqueado_ate")
        batch_op.drop_column("webchat_tentativas_cpf")
