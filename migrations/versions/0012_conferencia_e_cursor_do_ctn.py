"""a conferência da cobrança no contato, o motivo do encerramento e o cursor do CTN

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-09 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.add_column(sa.Column("conferida_em", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("meses_em_aberto", sa.JSON(none_as_null=True), nullable=True))
        batch_op.add_column(sa.Column("encerrada_motivo", sa.String(length=40), nullable=True))
    op.create_table(
        "cursor_ctn",
        sa.Column("nome", sa.String(length=64), nullable=False),
        sa.Column("valor", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("nome"),
    )


def downgrade() -> None:
    op.drop_table("cursor_ctn")
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.drop_column("encerrada_motivo")
        batch_op.drop_column("meses_em_aberto")
        batch_op.drop_column("conferida_em")
