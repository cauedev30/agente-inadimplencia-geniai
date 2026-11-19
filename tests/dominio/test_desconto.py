import pytest

from agente.dominio.desconto import Oferta, proxima_oferta, valor_com_desconto
from agente.politica import Regua


def _com_valor_final(politica, regua, valor_final_centavos):
    parametros = politica.reguas[regua].model_copy(
        update={"valor_final_centavos": valor_final_centavos}
    )
    return politica.model_copy(update={"reguas": {**politica.reguas, regua: parametros}})


@pytest.mark.parametrize(("divida", "desconto"), [(10020, 1020), (13360, 4360)])
def test_r3_oferece_o_valor_final_de_noventa_reais(politica, divida, desconto):
    assert proxima_oferta(Regua.R3, divida, 0, politica) == Oferta(
        degrau=1, desconto_centavos=desconto, valor_final_centavos=9000
    )


def test_a_oferta_e_uma_so(politica):
    assert proxima_oferta(Regua.R3, 10020, 1, politica) is None


@pytest.mark.parametrize("regua", [Regua.NR, Regua.R1, Regua.R2, Regua.PREVIA, Regua.R4, Regua.R6])
def test_regua_sem_valor_final_nao_tem_oferta(politica, regua):
    assert proxima_oferta(regua, 20040, 0, politica) is None
    assert valor_com_desconto(regua, 20040, 1, politica) == 20040


@pytest.mark.parametrize("divida", [9000, 8000, 1])
def test_valor_final_que_nao_baixa_a_divida_nao_gera_oferta(politica, divida):
    assert proxima_oferta(Regua.R3, divida, 0, politica) is None
    assert valor_com_desconto(Regua.R3, divida, 1, politica) == divida


def test_valor_com_desconto(politica):
    assert valor_com_desconto(Regua.R3, 10020, 0, politica) == 10020
    assert valor_com_desconto(Regua.R3, 10020, 1, politica) == 9000


def test_o_valor_final_vale_para_qualquer_regua_que_o_tenha(politica):
    outra = _com_valor_final(politica, Regua.R5, 12000)
    assert proxima_oferta(Regua.R5, 16700, 0, outra) == Oferta(1, 4700, 12000)


def test_degrau_negativo_e_erro(politica):
    with pytest.raises(ValueError, match="degrau"):
        valor_com_desconto(Regua.R3, 10020, -1, politica)


@pytest.mark.parametrize("degrau", [2, 3])
def test_degrau_da_politica_antiga_vale_a_oferta_feita(politica, degrau):
    assert valor_com_desconto(Regua.R3, 10020, degrau, politica) == 9000


@pytest.mark.parametrize("divida", [0, -500])
def test_divida_sem_valor_nao_gera_desconto_negativo(politica, divida):
    assert proxima_oferta(Regua.R3, divida, 0, politica) is None
    assert valor_com_desconto(Regua.R3, divida, 1, politica) == divida
