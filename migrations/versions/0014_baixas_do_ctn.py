"""as baixas que o CTN registrou, lidas pela rotina das baixas externas

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-09 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agente.db.modelos

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "baixa_ctn",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("id_externo", sa.String(length=64), nullable=False),
        sa.Column("matricula", sa.String(length=64), nullable=False),
        sa.Column("mes_ref", sa.String(length=7), nullable=False),
        sa.Column("valor_centavos", sa.Integer(), nullable=False),
        sa.Column(
            "forma",
            sa.Enum(
                "PIX",
                "CARTAO",
                "BOLETO",
                "ENERGIA",
                "OUTRO",
                name="formahistorico",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("pago_em", agente.db.modelos.MomentoUTC(), nullable=False),
        sa.Column("coletado_em", agente.db.modelos.MomentoUTC(), nullable=False),
        sa.Column("cobranca_id", sa.Integer(), nullable=True),
        sa.Column("aplicada_em", agente.db.modelos.MomentoUTC(), nullable=True),
        sa.Column("efeito", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(["cobranca_id"], ["cobranca.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id_externo"),
    )
    with op.batch_alter_table("baixa_ctn", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_baixa_ctn_matricula"), ["matricula"])


def downgrade() -> None:
    op.drop_table("baixa_ctn")
