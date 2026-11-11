"""O `alembic upgrade head` no Postgres, num banco vazio (o que o contêiner faz ao subir)."""

from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from agente.db.modelos import Base
from tests.conftest import alembic_config
from tests.postgres.conftest import URL


def test_upgrade_head_num_banco_vazio_cria_as_tabelas_e_para_na_ultima_revisao(motor_vazio):
    """Prova que as migrações rodam em ordem no Postgres (sem o modo batch do SQLite), que as
    tabelas dos modelos existem e que o banco fica marcado na revisão mais nova do código."""
    config = alembic_config(URL)
    assert inspect(motor_vazio).get_table_names() == []

    command.upgrade(config, "head")

    tabelas = set(inspect(motor_vazio).get_table_names())
    assert set(Base.metadata.tables) <= tabelas
    assert {"conversa", "mensagem", "turno", "cliente", "cobranca", "pagamento"} <= tabelas
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    with motor_vazio.connect() as conexao:
        atual = MigrationContext.configure(conexao).get_current_revision()
    assert atual == scripts.get_current_head()


def test_o_indice_parcial_de_uma_cobranca_ativa_por_cliente_existe_no_postgres(motor):
    indices = {i["name"]: i for i in inspect(motor).get_indexes("cobranca")}

    indice = indices["uq_cobranca_ativa_por_cliente"]
    assert indice["unique"] is True
    assert indice["column_names"] == ["cliente_id"]
    assert "ATIVA" in indice["dialect_options"]["postgresql_where"]


def test_a_matricula_do_cliente_e_unica_no_postgres(motor):
    restricoes = inspect(motor).get_unique_constraints("cliente")

    assert ["matricula_ctn"] in [r["column_names"] for r in restricoes]
