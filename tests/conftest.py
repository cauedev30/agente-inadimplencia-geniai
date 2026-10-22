from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig

from agente.db.modelos import Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.politica import Regua, carregar_politica

RAIZ = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> AlembicConfig:
    config = AlembicConfig(str(RAIZ / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configurar_log"] = False
    return config


@pytest.fixture
def politica():
    return carregar_politica(RAIZ / "politica.toml")


@pytest.fixture
def sessao():
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as s:
        yield s


@pytest.fixture
def conversa(sessao):
    """Uma conversa de uma cobrança ativa, com dados fictícios."""
    cliente = Cliente(matricula_ctn="M-0001", nome="Cliente Fictício", telefone="+5583900000001")
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha="teste",
    )
    conversa = Conversa(cobranca=cobranca, criada_em=datetime(2026, 10, 22, 12, tzinfo=UTC))
    sessao.add(conversa)
    sessao.commit()
    return conversa
