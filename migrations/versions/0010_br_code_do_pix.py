"""br_code do Pix gravado no pagamento

Revision ID: 0010
Revises: 0008
Create Date: 2026-10-23 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pagamento", schema=None) as batch_op:
        batch_op.add_column(sa.Column("br_code", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pagamento", schema=None) as batch_op:
        batch_op.drop_column("br_code")
