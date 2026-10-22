"""Desconto em degraus até o máximo da régua (D29, D35).

O desconto só vale no link do CTN (D34); quem decide usar este cálculo é a regra da ação, não aqui.
"""

from dataclasses import dataclass

from agente.politica import Politica, Regua


@dataclass(frozen=True)
class Oferta:
    degrau: int
    desconto_centavos: int
    valor_final_centavos: int


def _desconto(regua: Regua, divida_centavos: int, degrau: int, politica: Politica) -> int:
    parametros = politica.reguas[regua]
    if not 0 <= degrau <= parametros.n_degraus:
        raise ValueError(f"degrau {degrau} fora da régua {regua} (0 a {parametros.n_degraus})")
    desconto = parametros.desconto_max_centavos * degrau // parametros.n_degraus
    # O desconto nunca zera a dívida.
    return min(desconto, divida_centavos - 1)


def valor_com_desconto(regua: Regua, divida_centavos: int, degrau: int, politica: Politica) -> int:
    """O valor a pagar no degrau. O degrau 0 é o valor integral."""
    return divida_centavos - _desconto(regua, divida_centavos, degrau, politica)


def proxima_oferta(
    regua: Regua, divida_centavos: int, degrau_atual: int, politica: Politica
) -> Oferta | None:
    """O próximo degrau, ou None na régua sem desconto ou com os degraus esgotados."""
    parametros = politica.reguas[regua]
    if parametros.desconto_max_centavos == 0 or degrau_atual >= parametros.n_degraus:
        return None
    degrau = degrau_atual + 1
    desconto = _desconto(regua, divida_centavos, degrau, politica)
    return Oferta(degrau, desconto, divida_centavos - desconto)
