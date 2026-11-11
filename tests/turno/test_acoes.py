"""A tabela de anexos: o que vai junto da ação principal (arquitetura, seção 4; D37)."""

import pytest

from agente.db.modelos import Cobranca
from agente.politica import Regua
from agente.turno.acoes import Acao, anexar


def _cobranca(regua=Regua.R3, mensalidades=3, propenso=False):
    return Cobranca(
        regua=regua,
        mensalidades_abertas=mensalidades,
        divida_centavos=3340 * mensalidades,
        propenso_cartao=propenso,
    )


# Tabela de anexos (D37).


def test_inform_debt_do_propenso_leva_o_link():
    assert anexar(Acao.INFORM_DEBT, _cobranca(propenso=True)) == [Acao.SEND_CTN_LINK]


def test_inform_debt_do_nao_propenso_leva_o_pix():
    assert anexar(Acao.INFORM_DEBT, _cobranca(propenso=False)) == [Acao.GENERATE_PIX]


@pytest.mark.parametrize("propenso", [True, False])
def test_offer_discount_sempre_leva_o_link(propenso):
    assert anexar(Acao.OFFER_DISCOUNT, _cobranca(propenso=propenso)) == [Acao.SEND_CTN_LINK]


@pytest.mark.parametrize(
    "acao",
    [Acao.SEND_CTN_LINK, Acao.GENERATE_PIX, Acao.OUT_OF_SCOPE, Acao.NONE, Acao.HUMAN],
)
@pytest.mark.parametrize("propenso", [True, False])
def test_as_outras_acoes_vao_sem_anexo(acao, propenso):
    assert anexar(acao, _cobranca(propenso=propenso)) == []
