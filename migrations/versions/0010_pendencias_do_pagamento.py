"""o que falta fazer depois da confirmação do pagamento e o valor divergente

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pagamento", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pendencias", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(
            sa.Column("tentativas_conclusao", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("proxima_tentativa_em", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("atencao_em", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("divergente_centavos", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_pagamento_proxima_tentativa_em"), ["proxima_tentativa_em"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("pagamento", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pagamento_proxima_tentativa_em"))
        batch_op.drop_column("divergente_centavos")
        batch_op.drop_column("atencao_em")
        batch_op.drop_column("proxima_tentativa_em")
        batch_op.drop_column("tentativas_conclusao")
        batch_op.drop_column("pendencias")
