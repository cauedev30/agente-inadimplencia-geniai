from datetime import date

from agente.dominio.propensao import eh_propenso
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
