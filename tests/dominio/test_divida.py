import pytest

from agente.dominio.divida import calcular_divida, classificar_regua
from agente.politica import Regua


@pytest.mark.parametrize(
    ("mensalidades", "regua"),
    [(1, Regua.R1), (2, Regua.R2), (3, Regua.R3), (4, Regua.R4), (5, Regua.R5), (6, Regua.R6)],
)
def test_regua_pelo_numero_de_mensalidades(politica, mensalidades, regua):
    assert classificar_regua(mensalidades, None, politica) is regua


def test_acima_de_6_usa_a_regua_da_politica(politica):
    assert classificar_regua(7, None, politica) is Regua.R6
    assert classificar_regua(12, None, politica) is Regua.R6


@pytest.mark.parametrize("informada", [Regua.NR, Regua.PREVIA, Regua.R2])
def test_regua_informada_pela_lista_vence(politica, informada):
    assert classificar_regua(3, informada, politica) is informada


@pytest.mark.parametrize("mensalidades", [0, -1])
def test_sem_mensalidade_em_aberto_e_erro(politica, mensalidades):
    with pytest.raises(ValueError, match="mensalidade"):
        classificar_regua(mensalidades, None, politica)


@pytest.mark.parametrize(("mensalidades", "divida"), [(1, 3340), (3, 10020), (6, 20040)])
def test_divida_e_mensalidade_vezes_meses(politica, mensalidades, divida):
    assert calcular_divida(mensalidades, politica) == divida


def test_divida_sem_mensalidade_e_erro(politica):
    with pytest.raises(ValueError, match="mensalidade"):
        calcular_divida(0, politica)
