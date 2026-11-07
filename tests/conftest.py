from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig

from agente.db.modelos import Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.politica import Regua, carregar_politica

RAIZ = Path(__file__).resolve().parents[1]

VARIAVEIS_DO_JEV = (
    "AGENTE_JEV_API_KEY",
    "AGENTE_JEV_URL",
    "AGENTE_JEV_MODELO",
    "AGENTE_JEV_DADOS_REAIS_LIBERADOS",
)

VARIAVEIS_DO_CTN = (
    "AGENTE_CTN_URL",
    "AGENTE_CTN_API_KEY",
    "AGENTE_CTN_UNIDADE",
    "AGENTE_LLM_DADOS_REAIS_LIBERADOS",
)


@pytest.fixture(autouse=True)
def _sem_ctn_real_no_ambiente(monkeypatch):
    """Uma chave do CTN real exportada no shell não pode ligar a EFICAZ nos outros testes."""
    for variavel in VARIAVEIS_DO_CTN:
        monkeypatch.delenv(variavel, raising=False)


def limpar_ambiente_do_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tira as variáveis do Jev real do ambiente do processo."""
    for variavel in VARIAVEIS_DO_JEV:
        monkeypatch.delenv(variavel, raising=False)


@pytest.fixture(autouse=True)
def _sem_jev_real_no_ambiente(monkeypatch):
    """Exportar a chave para o teste real (AGENTE_JEV_API_KEY) não pode ligar o Jev real nos outros
    testes: `Config()` lê o ambiente, e o texto dos fixtures iria à rede. Quem precisa das
    variáveis as define com `monkeypatch.setenv` dentro do teste."""
    limpar_ambiente_do_jev(monkeypatch)


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
