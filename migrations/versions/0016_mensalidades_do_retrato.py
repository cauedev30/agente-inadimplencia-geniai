"""os meses em atraso do retrato, que a conferência não reescreve

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-09 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mensalidades_retrato", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.drop_column("mensalidades_retrato")
