from datetime import date

from agente.dominio.propensao import (
    eh_propenso,
    meses_do_historico,
    pagamentos_ate_o_ult_mesref,
)
from agente.tipos import FormaHistorico, PagamentoHistorico

PIX = FormaHistorico.PIX


def _pagamentos(*itens):
    return [
        PagamentoHistorico(data=data, forma=forma, valor_centavos=3340) for data, forma in itens
    ]


def test_tres_pix_seguidos_no_mesmo_dia_e_propenso(politica):
    historico = _pagamentos(
        (date(2026, 7, 10), PIX), (date(2026, 8, 11), PIX), (date(2026, 9, 12), PIX)
    )
    assert eh_propenso(historico, politica) is True


def test_ordem_do_historico_nao_importa(politica):
    historico = _pagamentos(
        (date(2026, 9, 12), PIX), (date(2026, 7, 10), PIX), (date(2026, 8, 11), PIX)
    )
    assert eh_propenso(historico, politica) is True


def test_vale_so_os_ultimos_pagamentos(politica):
    historico = _pagamentos(
        (date(2026, 3, 2), FormaHistorico.BOLETO),
        (date(2026, 7, 10), PIX),
        (date(2026, 8, 11), PIX),
        (date(2026, 9, 12), PIX),
    )
    assert eh_propenso(historico, politica) is True


def test_virada_de_ano_conta_como_mes_seguido(politica):
    historico = _pagamentos(
        (date(2025, 11, 5), PIX), (date(2025, 12, 5), PIX), (date(2026, 1, 6), PIX)
    )
    assert eh_propenso(historico, politica) is True


def test_so_dois_meses_nao_e_propenso(politica):
    historico = _pagamentos((date(2026, 8, 10), PIX), (date(2026, 9, 10), PIX))
    assert eh_propenso(historico, politica) is False


def test_cartao_no_meio_nao_e_propenso(politica):
    historico = _pagamentos(
        (date(2026, 7, 10), PIX),
        (date(2026, 8, 10), FormaHistorico.CARTAO),
        (date(2026, 9, 10), PIX),
    )
    assert eh_propenso(historico, politica) is False


def test_mes_pulado_nao_e_propenso(politica):
    historico = _pagamentos(
        (date(2026, 6, 10), PIX), (date(2026, 8, 10), PIX), (date(2026, 9, 10), PIX)
    )
    assert eh_propenso(historico, politica) is False


def test_dois_pagamentos_no_mesmo_mes_nao_e_propenso(politica):
    historico = _pagamentos(
        (date(2026, 8, 1), PIX), (date(2026, 8, 20), PIX), (date(2026, 9, 10), PIX)
    )
    assert eh_propenso(historico, politica) is False


def test_dia_do_mes_muito_diferente_nao_e_propenso(politica):
    historico = _pagamentos(
        (date(2026, 7, 5), PIX), (date(2026, 8, 20), PIX), (date(2026, 9, 6), PIX)
    )
    assert eh_propenso(historico, politica) is False


def test_tolerancia_no_limite(politica):
    dentro = _pagamentos(
        (date(2026, 7, 10), PIX), (date(2026, 8, 13), PIX), (date(2026, 9, 11), PIX)
    )
    fora = _pagamentos((date(2026, 7, 10), PIX), (date(2026, 8, 14), PIX), (date(2026, 9, 11), PIX))
    assert eh_propenso(dentro, politica) is True
    assert eh_propenso(fora, politica) is False


def test_sem_historico_nao_e_propenso(politica):
    assert eh_propenso([], politica) is False


# ---- a janela até o `ult_mesref` (D58) ----

HOJE = date(2026, 11, 9)


def test_sem_ult_mesref_a_janela_e_a_de_hoje(politica):
    n = politica.propensao.n_meses
    assert meses_do_historico(politica, None, HOJE) == n + 1
    assert meses_do_historico(politica, date(2026, 7, 1), None) == n + 1


def test_a_janela_vai_de_hoje_ate_um_mes_antes_dos_n_pagamentos_do_ult_mesref(politica):
    n = politica.propensao.n_meses
    # Hoje é novembro e o último mês pago é julho: 4 meses de atraso até hoje, mais a janela.
    assert meses_do_historico(politica, date(2026, 7, 1), HOJE) == n + 1 + 4
    # O ult_mesref do mês corrente (ou de um mês futuro) é a janela de hoje.
    assert meses_do_historico(politica, date(2026, 11, 1), HOJE) == n + 1
    assert meses_do_historico(politica, date(2026, 12, 1), HOJE) == n + 1


def test_a_janela_atravessa_a_virada_de_ano(politica):
    n = politica.propensao.n_meses
    assert meses_do_historico(politica, date(2025, 11, 1), HOJE) == n + 1 + 12


def test_so_os_pagamentos_ate_o_fim_do_mes_do_ult_mesref_valem(politica):
    historico = _pagamentos(
        (date(2026, 6, 10), PIX),
        (date(2026, 7, 31), PIX),
        (date(2026, 8, 1), PIX),
        (date(2026, 10, 3), FormaHistorico.BOLETO),
    )
    ate = pagamentos_ate_o_ult_mesref(historico, date(2026, 7, 1))
    assert [p.data for p in ate] == [date(2026, 6, 10), date(2026, 7, 31)]
    assert pagamentos_ate_o_ult_mesref(historico, None) == historico
