import pytest

from agente.dominio.desconto import Oferta, oferta_no_degrau, proxima_oferta, valor_com_desconto
from agente.politica import Regua


def _degraus(politica, regua, divida):
    ofertas, degrau = [], 0
    while (oferta := proxima_oferta(regua, divida, degrau, politica)) is not None:
        ofertas.append(oferta)
        degrau = oferta.degrau
    return ofertas


def test_r3_libera_tres_degraus_ate_o_maximo(politica):
    assert _degraus(politica, Regua.R3, 10020) == [
        Oferta(degrau=1, desconto_centavos=3000, valor_final_centavos=7020),
        Oferta(degrau=2, desconto_centavos=6000, valor_final_centavos=4020),
        Oferta(degrau=3, desconto_centavos=9000, valor_final_centavos=1020),
    ]


@pytest.mark.parametrize(
    ("regua", "divida", "descontos"),
    [
        (Regua.R4, 13360, [3000, 6000, 9000]),
        (Regua.R5, 16700, [2500, 5000, 7500]),
        (Regua.R6, 20040, [2000, 4000, 6000]),
        (Regua.PREVIA, 10020, [2000, 4000, 6000]),
    ],
)
def test_degraus_das_outras_reguas(politica, regua, divida, descontos):
    assert [o.desconto_centavos for o in _degraus(politica, regua, divida)] == descontos


@pytest.mark.parametrize("regua", [Regua.NR, Regua.R1, Regua.R2])
def test_regua_sem_desconto_nao_tem_oferta(politica, regua):
    assert proxima_oferta(regua, 6680, 0, politica) is None


def test_depois_do_ultimo_degrau_nao_ha_oferta(politica):
    assert proxima_oferta(Regua.R3, 10020, 3, politica) is None


def test_degrau_que_nao_divide_exato_arredonda_para_baixo(politica):
    parametros = politica.reguas[Regua.R5].model_copy(update={"desconto_max_centavos": 1000})
    reguas = {**politica.reguas, Regua.R5: parametros}
    outra = politica.model_copy(update={"reguas": reguas})
    assert [o.desconto_centavos for o in _degraus(outra, Regua.R5, 16700)] == [333, 666, 1000]


def test_valor_final_nunca_fica_abaixo_de_um_centavo(politica):
    oferta = proxima_oferta(Regua.R3, 5000, 2, politica)
    assert oferta.desconto_centavos == 4999
    assert oferta.valor_final_centavos == 1


def test_valor_com_desconto(politica):
    assert valor_com_desconto(Regua.R3, 10020, 0, politica) == 10020
    assert valor_com_desconto(Regua.R3, 10020, 2, politica) == 4020


@pytest.mark.parametrize("degrau", [-1, 4])
def test_degrau_fora_da_regua_e_erro(politica, degrau):
    with pytest.raises(ValueError, match="degrau"):
        valor_com_desconto(Regua.R3, 10020, degrau, politica)


def test_oferta_no_degrau_e_a_que_ja_foi_feita(politica):
    assert oferta_no_degrau(Regua.R3, 10020, 2, politica) == Oferta(2, 6000, 4020)
    assert oferta_no_degrau(Regua.R3, 10020, 0, politica) is None


@pytest.mark.parametrize("divida", [0, -500])
def test_divida_sem_valor_nao_gera_desconto_negativo(politica, divida):
    oferta = oferta_no_degrau(Regua.R3, divida, 1, politica)

    assert oferta is not None
    assert oferta.desconto_centavos == 0
    assert valor_com_desconto(Regua.R3, divida, 1, politica) == divida
