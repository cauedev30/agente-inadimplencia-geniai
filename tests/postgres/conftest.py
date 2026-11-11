"""O Postgres de verdade nos testes (tarefa 2.2 do enxugamento).

Estes testes só rodam com `AGENTE_TESTE_POSTGRES_URL` (formato
`postgresql+psycopg://usuario:senha@host:5432/agente_teste`); sem ela, todos são pulados e a
suíte normal segue sem Postgres. A coleta nunca conecta: só os fixtures abrem conexão.

Cada teste começa num banco vazio (o schema `public` é derrubado e recriado) e migrado até o head
pelo Alembic, como o contêiner faz ao subir. Por isso o banco tem de ser um banco de teste: o
fixture se recusa a rodar se o nome dele não termina em `_teste`.

O `sessao` daqui troca o SQLite em memória do `tests/conftest.py` pelo Postgres migrado; as demais
fixtures do modo teste (`casos`, `deps`, `conversa_de_teste`, `testador`) são as de `tests/teste/`.
"""

import os

import pytest
from alembic import command
from sqlalchemy import make_url, text
from sqlalchemy.orm import sessionmaker

from agente.db.sessao import criar_motor
from tests.conftest import alembic_config
from tests.teste.conftest import casos, conversa_de_teste, deps, testador  # noqa: F401

URL = os.environ.get("AGENTE_TESTE_POSTGRES_URL")


@pytest.fixture(autouse=True)
def _exige_o_postgres():
    if not URL:
        pytest.skip("sem AGENTE_TESTE_POSTGRES_URL (os testes do Postgres só rodam com ela)")
    banco = make_url(URL).database or ""
    if not banco.endswith("_teste"):
        pytest.fail(f"o banco '{banco}' não termina em _teste: o fixture derruba o schema public")


@pytest.fixture
def motor_vazio():
    """O motor do banco de teste, com o schema `public` recriado: nenhuma tabela."""
    motor = criar_motor(URL)
    with motor.begin() as conexao:
        conexao.execute(text("DROP SCHEMA public CASCADE"))
        conexao.execute(text("CREATE SCHEMA public"))
    yield motor
    motor.dispose()


@pytest.fixture
def motor(motor_vazio):
    """O motor do banco de teste migrado até o head (`alembic upgrade head`)."""
    command.upgrade(alembic_config(URL), "head")
    return motor_vazio


@pytest.fixture
def fabrica(motor):
    """Abre sessões novas no Postgres migrado (para os testes com mais de uma conexão)."""
    return sessionmaker(bind=motor, expire_on_commit=False)


@pytest.fixture
def sessao(fabrica):
    with fabrica() as s:
        yield s
