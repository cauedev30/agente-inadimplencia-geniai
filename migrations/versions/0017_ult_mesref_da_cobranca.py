"""o ult_mesref do retrato, para a conferência contar só os meses depois dele

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ult_mesref", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cobranca", schema=None) as batch_op:
        batch_op.drop_column("ult_mesref")
