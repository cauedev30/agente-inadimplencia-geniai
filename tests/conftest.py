from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig

from agente.db.modelos import Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.politica import LiberacaoPiloto, Regua, carregar_politica

RAIZ = Path(__file__).resolve().parents[1]

VARIAVEIS_DO_CTN = (
    "AGENTE_CTN_URL",
    "AGENTE_CTN_API_KEY",
    "AGENTE_CTN_UNIDADE",
    "AGENTE_LLM_API_KEY",
    "AGENTE_LLM_DADOS_REAIS_LIBERADOS",
)


@pytest.fixture(autouse=True)
def _sem_ctn_real_no_ambiente(monkeypatch):
    """Uma chave do CTN real ou do Luna exportada no shell não pode ligar a EFICAZ nem a OpenAI nos
    outros testes: `Config()` lê o ambiente. Quem precisa das variáveis as define com
    `monkeypatch.setenv` dentro do teste."""
    for variavel in VARIAVEIS_DO_CTN:
        monkeypatch.delenv(variavel, raising=False)


def alembic_config(url: str) -> AlembicConfig:
    config = AlembicConfig(str(RAIZ / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configurar_log"] = False
    return config


@pytest.fixture
def politica():
    return carregar_politica(RAIZ / "politica.toml")


# Os telefones das cobranças que os testes de disparo criam (`+558399999<n>`, de 1 a 199).
TELEFONES_DAS_COBRANCAS = tuple(f"+558399999{n:04d}" for n in range(1, 200))


def piloto_com(politica, *telefones):
    """A mesma política com estes telefones no piloto (D61): só eles conversam com o agente."""
    return politica.model_copy(
        update={"liberacao": LiberacaoPiloto(telefones=frozenset(telefones))}
    )


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


# O modo teste (D56): a política com o `[teste]`, o piloto de 2 testadores e o cardápio dos 5 casos.
TESTADOR_1 = "+5583988887777"  # com o 9; a variante sem ele é +558388887777 (A1)
TESTADOR_2 = "+5583999990002"
TESTADORES = (TESTADOR_1, TESTADOR_2)


def escrever_politica_de_teste(pasta: Path) -> Path:
    """Uma cópia do politica.toml com o piloto dos 2 testadores e o `[teste]`, na `pasta`."""
    texto = (RAIZ / "politica.toml").read_text(encoding="utf-8")
    piloto = f'liberacao = {{ modo = "piloto", telefones = ["{TESTADOR_1}", "{TESTADOR_2}"] }}'
    texto = texto.replace('liberacao = { modo = "piloto", telefones = [] }', piloto, 1)
    texto += (
        '\n[teste]\ncardapio = "cardapio-teste.toml"\n'
        'texto_abertura = "Oi, {nome}! Há {mensalidades} mensalidades em aberto, {valor}."\n'
    )
    caminho = pasta / "politica.toml"
    caminho.write_text(texto, encoding="utf-8")
    (pasta / "cardapio-teste.toml").write_text(
        (RAIZ / "cardapio-teste.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return caminho


def ligar_o_disparo(pasta: Path, monkeypatch, *telefones: str) -> Path:
    """O ambiente do `agente disparar`: uma política com `telefones` no piloto (o disparo só abre as
    cobranças do piloto), numa cópia na `pasta`."""
    texto = (RAIZ / "politica.toml").read_text(encoding="utf-8")
    lista = ", ".join(f'"{t}"' for t in telefones)
    texto = texto.replace(
        'liberacao = { modo = "piloto", telefones = [] }',
        f'liberacao = {{ modo = "piloto", telefones = [{lista}] }}',
        1,
    )
    caminho = pasta / "politica-liberada.toml"
    caminho.write_text(texto, encoding="utf-8")
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(caminho))
    return caminho


@pytest.fixture
def politica_de_teste(tmp_path):
    return carregar_politica(escrever_politica_de_teste(tmp_path))
