"""as notas do testador (/nota) do modo teste

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agente.db.modelos

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nota_teste",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversa_id", sa.Integer(), nullable=False),
        sa.Column("cobranca_id", sa.Integer(), nullable=False),
        sa.Column("turno_id", sa.Integer(), nullable=True),
        sa.Column("mensagem_id", sa.Integer(), nullable=False),
        sa.Column("caso", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("criada_em", agente.db.modelos.MomentoUTC(), nullable=False),
        sa.ForeignKeyConstraint(["cobranca_id"], ["cobranca.id"]),
        sa.ForeignKeyConstraint(["conversa_id"], ["conversa.id"]),
        sa.ForeignKeyConstraint(["mensagem_id"], ["mensagem.id"]),
        sa.ForeignKeyConstraint(["turno_id"], ["turno.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mensagem_id"),
    )
    with op.batch_alter_table("nota_teste", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_nota_teste_conversa_id"), ["conversa_id"])
        batch_op.create_index(batch_op.f("ix_nota_teste_criada_em"), ["criada_em"])


def downgrade() -> None:
    op.drop_table("nota_teste")
