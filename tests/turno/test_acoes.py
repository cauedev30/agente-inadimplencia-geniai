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


# Tabela de anexos (D37), com e sem meio de pagamento vigente (plano de autonomia, tarefa 1.2).


@pytest.mark.parametrize("acao", [Acao.INFORM_DEBT, Acao.ANSWER, Acao.OUT_OF_SCOPE])
def test_sem_meio_vigente_o_propenso_leva_o_link(acao):
    anexos = anexar(acao, _cobranca(propenso=True), tem_meio_vigente=False)
    assert anexos == [Acao.SEND_CTN_LINK]


@pytest.mark.parametrize("acao", [Acao.INFORM_DEBT, Acao.ANSWER, Acao.OUT_OF_SCOPE])
def test_sem_meio_vigente_o_nao_propenso_leva_o_pix(acao):
    anexos = anexar(acao, _cobranca(propenso=False), tem_meio_vigente=False)
    assert anexos == [Acao.GENERATE_PIX]


@pytest.mark.parametrize("acao", [Acao.INFORM_DEBT, Acao.ANSWER, Acao.OUT_OF_SCOPE])
@pytest.mark.parametrize("propenso", [True, False])
def test_com_meio_vigente_a_informacao_a_resposta_e_o_fora_do_assunto_vao_sem_anexo(acao, propenso):
    assert anexar(acao, _cobranca(propenso=propenso), tem_meio_vigente=True) == []


@pytest.mark.parametrize("propenso", [True, False])
@pytest.mark.parametrize("tem_meio", [True, False])
def test_offer_discount_sempre_leva_o_meio_da_regra_mesmo_com_um_vigente(propenso, tem_meio):
    anexos = anexar(Acao.OFFER_DISCOUNT, _cobranca(propenso=propenso), tem_meio_vigente=tem_meio)
    assert anexos == [Acao.SEND_CTN_LINK if propenso else Acao.GENERATE_PIX]


@pytest.mark.parametrize(
    "acao",
    [Acao.SEND_CTN_LINK, Acao.GENERATE_PIX, Acao.NONE, Acao.HUMAN],
)
@pytest.mark.parametrize("propenso", [True, False])
@pytest.mark.parametrize("tem_meio", [True, False])
def test_as_outras_acoes_vao_sem_anexo(acao, propenso, tem_meio):
    assert anexar(acao, _cobranca(propenso=propenso), tem_meio_vigente=tem_meio) == []
