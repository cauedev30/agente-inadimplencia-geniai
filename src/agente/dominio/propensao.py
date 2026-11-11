"""Propensão ao cartão (D36): quem já paga Pix todo mês no mesmo dia tem uma recorrência pronta."""

from datetime import date
from itertools import pairwise

from agente.politica import Politica
from agente.tipos import FormaHistorico, PagamentoHistorico


def _indice(dia: date) -> int:
    return dia.year * 12 + dia.month


def _indice_do_mes(pagamento: PagamentoHistorico) -> int:
    return _indice(pagamento.data)


def meses_do_historico(politica: Politica, ult_mesref: date | None, hoje: date | None) -> int:
    """Quantos meses, contados de hoje, o histórico de pagamentos tem de cobrir (D58).

    A propensão olha como a pessoa pagava ANTES de atrasar, até o mês do `ult_mesref`: quem deve 3
    meses ou mais nunca tem `n_meses` pagamentos seguidos na janela de hoje. A janela vai de hoje
    até um mês antes do primeiro dos `n_meses` pagamentos que terminam no `ult_mesref` (o mês a
    mais cobre o mês corrente, que pode não ter pagamento ainda). Sem `ult_mesref` (CSV, cobrança
    antiga) ou sem a data de hoje, é a janela de hoje: `n_meses + 1`."""
    n_meses = politica.propensao.n_meses
    if ult_mesref is None or hoje is None:
        return n_meses + 1
    return n_meses + 1 + max(0, _indice(hoje) - _indice(ult_mesref))


def pagamentos_ate_o_ult_mesref(
    historico: list[PagamentoHistorico], ult_mesref: date | None
) -> list[PagamentoHistorico]:
    """Os pagamentos até o fim do mês do `ult_mesref`; os de depois (pagamento parcial do atraso)
    ficam de fora e não estragam a sequência de antes. Sem `ult_mesref`, todos."""
    if ult_mesref is None:
        return historico
    limite = _indice(ult_mesref)
    return [p for p in historico if _indice(p.data) <= limite]


def eh_propenso(historico: list[PagamentoHistorico], politica: Politica) -> bool:
    """Os últimos n_meses pagamentos foram Pix, em meses seguidos, com o dia do mês parecido.

    Sem histórico suficiente, o cliente conta como não propenso.
    """
    n_meses = politica.propensao.n_meses
    ultimos = sorted(historico, key=lambda p: p.data)[-n_meses:]
    if len(ultimos) < n_meses:
        return False
    if any(p.forma is not FormaHistorico.PIX for p in ultimos):
        return False
    meses = [_indice_do_mes(p) for p in ultimos]
    if any(seguinte - anterior != 1 for anterior, seguinte in pairwise(meses)):
        return False
    dias = [p.data.day for p in ultimos]
    return max(dias) - min(dias) <= politica.propensao.tolerancia_dias
