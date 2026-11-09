"""índices de baixa_ctn.aplicada_em e baixa_ctn.efeito, filtrados a cada 10 minutos

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-09 22:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("baixa_ctn", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_baixa_ctn_aplicada_em"), ["aplicada_em"])
        batch_op.create_index(batch_op.f("ix_baixa_ctn_efeito"), ["efeito"])


def downgrade() -> None:
    with op.batch_alter_table("baixa_ctn", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_baixa_ctn_efeito"))
        batch_op.drop_index(batch_op.f("ix_baixa_ctn_aplicada_em"))
