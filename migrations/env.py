"""Ambiente do Alembic: aplica as migrações no banco de AGENTE_DATABASE_URL."""

from logging.config import fileConfig

from alembic import context

from agente.config import carregar_config
from agente.db.modelos import Base
from agente.db.sessao import criar_motor

config = context.config
if config.config_file_name is not None and config.attributes.get("configurar_log", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or carregar_config().database_url


def _opcoes(url: str) -> dict:
    # O SQLite não altera colunas no lugar: o modo batch recria a tabela.
    return {"target_metadata": Base.metadata, "render_as_batch": url.startswith("sqlite")}


def rodar_offline() -> None:
    url = _url()
    context.configure(url=url, literal_binds=True, **_opcoes(url))
    with context.begin_transaction():
        context.run_migrations()


def rodar_online() -> None:
    url = _url()
    motor = criar_motor(url)
    with motor.connect() as conexao:
        context.configure(connection=conexao, **_opcoes(url))
        with context.begin_transaction():
            context.run_migrations()
    motor.dispose()


if context.is_offline_mode():
    rodar_offline()
else:
    rodar_online()
