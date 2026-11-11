"""As fixtures do modo teste: o `CTNDeCasos`, as dependências, o testador 1 no caso 1 e o
`Mandante` que fala por ele."""

import pytest

from agente.db.modelos import Conversa
from agente.integracoes.ctn_teste import CTNDeCasos
from agente.teste.casos import ativar_caso
from tests.conftest import TESTADOR_1
from tests.teste.auxiliares import CHATWOOT_ID, Mandante, montar_deps
from tests.turno.auxiliares import SEGUNDA_10H


@pytest.fixture
def casos(politica_de_teste):
    return CTNDeCasos(
        politica_de_teste.teste.casos,
        politica_de_teste.mensalidade_centavos,
        lambda: SEGUNDA_10H.date(),
    )


@pytest.fixture
def deps(politica_de_teste, casos):
    return montar_deps(politica_de_teste, casos)


@pytest.fixture
def conversa_de_teste(sessao, politica_de_teste, casos):
    """O testador 1 no caso 1, com a conversa ligada ao Chatwoot e a conferência de hoje."""
    cobranca = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 1, SEGUNDA_10H)
    conversa = Conversa(cobranca=cobranca, chatwoot_conversa_id=CHATWOOT_ID)
    sessao.add(conversa)
    sessao.commit()
    return conversa


@pytest.fixture
def testador(sessao, conversa_de_teste):
    return Mandante(sessao, conversa_de_teste)
