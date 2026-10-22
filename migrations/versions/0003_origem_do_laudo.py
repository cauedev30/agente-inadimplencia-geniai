"""origem do laudo

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 19:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Antes do Jev real (T10), todo laudo gravado veio da falsa.
    with op.batch_alter_table("laudo", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("origem", sa.String(length=64), nullable=False, server_default="falso")
        )


def downgrade() -> None:
    with op.batch_alter_table("laudo", schema=None) as batch_op:
        batch_op.drop_column("origem")
