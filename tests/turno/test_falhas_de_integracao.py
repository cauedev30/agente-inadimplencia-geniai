"""O turno trata como integração fora do ar o que as interfaces levantam na falha."""

import pytest

from agente.integracoes.ctn import ClienteNaoEncontrado, CTNIndisponivel
from agente.integracoes.pix import PixIndisponivel
from agente.turno.orquestrador import _FALHAS_DE_INTEGRACAO


@pytest.mark.parametrize("erro", [PixIndisponivel, CTNIndisponivel, ClienteNaoEncontrado])
def test_o_turno_trata_a_falha_das_interfaces_como_integracao(erro):
    assert issubclass(erro, _FALHAS_DE_INTEGRACAO)
