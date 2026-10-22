"""Propensão ao cartão (D36): quem já paga Pix todo mês no mesmo dia tem uma recorrência pronta."""

from itertools import pairwise

from agente.politica import Politica
from agente.tipos import FormaHistorico, PagamentoHistorico


def _indice_do_mes(pagamento: PagamentoHistorico) -> int:
    return pagamento.data.year * 12 + pagamento.data.month


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
