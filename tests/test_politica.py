from datetime import date, time

import pytest

from agente.politica import PoliticaInvalida, Regua, carregar_politica

from .conftest import RAIZ

ARQUIVO_PADRAO = RAIZ / "politica.toml"


def test_arquivo_padrao_carrega(politica):
    assert politica.mensalidade_centavos == 3340
    assert politica.teto_resolucao == 5
    assert politica.regua_acima_de_6 is Regua.R6
    assert politica.confianca_minima == 0.60


@pytest.mark.parametrize(
    ("regua", "desconto"),
    [
        (Regua.NR, 0),
        (Regua.R1, 0),
        (Regua.R2, 0),
        (Regua.R3, 9000),
        (Regua.PREVIA, 6000),
        (Regua.R4, 9000),
        (Regua.R5, 7500),
        (Regua.R6, 6000),
    ],
)
def test_desconto_maximo_por_regua(politica, regua, desconto):
    assert politica.reguas[regua].desconto_max_centavos == desconto
    assert politica.reguas[regua].n_degraus == 3


def test_janela_padrao(politica):
    janela = politica.janela
    assert janela.fuso == "America/Fortaleza"
    assert janela.dias["seg"].inicio == time(8, 0)
    assert janela.dias["sex"].fim == time(18, 0)
    assert janela.dias["sab"].fim == time(12, 0)
    assert "dom" not in janela.dias
    assert date(2026, 12, 25) in janela.feriados


def test_propensao_e_limiares(politica):
    assert politica.propensao.n_meses == 3
    assert politica.propensao.tolerancia_dias == 3
    assert politica.limiares_jev1["quer_retencao"] == 0.80
    assert politica.limiares_jev2["GENERATE_PIX"] == 0.90


def _arquivo_com(tmp_path, original, novo):
    texto = ARQUIVO_PADRAO.read_text(encoding="utf-8")
    assert original in texto
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto.replace(original, novo, 1), encoding="utf-8")
    return caminho


@pytest.mark.parametrize(
    ("original", "novo", "campo"),
    [
        ("desconto_max_centavos = 9000", "desconto_max_centavos = -1", "desconto_max_centavos"),
        ("n_degraus = 3", "n_degraus = 0", "n_degraus"),
        ("teto_resolucao = 5", "teto_resolucao = 0", "teto_resolucao"),
        ("quer_retencao = 0.80", "quer_retencao = 1.5", "quer_retencao"),
        ('fuso = "America/Fortaleza"', 'fuso = "America/Lugar_Nenhum"', "fuso"),
        ('sab = ["08:00", "12:00"]', 'sab = ["12:00", "08:00"]', "sab"),
    ],
)
def test_valor_invalido_diz_o_campo(tmp_path, original, novo, campo):
    with pytest.raises(PoliticaInvalida, match=campo):
        carregar_politica(_arquivo_com(tmp_path, original, novo))


def test_regua_faltando_e_recusada(tmp_path):
    caminho = _arquivo_com(tmp_path, "[reguas.R5]", "[reguas.R5_FORA]")
    with pytest.raises(PoliticaInvalida, match="reguas"):
        carregar_politica(caminho)
