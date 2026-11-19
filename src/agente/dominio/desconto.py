"""O valor final da régua e a oferta única (D64).

A régua traz o valor final; a oferta é uma só e vale no Pix e no link. Quem decide usar este
cálculo é a regra da ação, não aqui.
"""

from dataclasses import dataclass

from agente.politica import Politica, Regua


@dataclass(frozen=True)
class Oferta:
    degrau: int
    desconto_centavos: int
    valor_final_centavos: int


def _alvo(regua: Regua, divida_centavos: int, politica: Politica) -> int | None:
    """O valor final da régua, se existe e é menor que a dívida; senão None (sem desconto)."""
    alvo = politica.reguas[regua].valor_final_centavos
    return alvo if alvo is not None and alvo < divida_centavos else None


def valor_com_desconto(regua: Regua, divida_centavos: int, degrau: int, politica: Politica) -> int:
    """O valor a pagar no degrau: o degrau 0 é a dívida; o 1 é o valor final da régua."""
    if degrau not in (0, 1):
        raise ValueError(f"degrau {degrau} fora da régua {regua} (0 ou 1)")
    alvo = _alvo(regua, divida_centavos, politica) if degrau == 1 else None
    return divida_centavos if alvo is None else alvo


def proxima_oferta(
    regua: Regua, divida_centavos: int, degrau_atual: int, politica: Politica
) -> Oferta | None:
    """A oferta única, ou None: já feita, régua sem valor final ou valor que não baixa a dívida."""
    if degrau_atual != 0:
        return None
    alvo = _alvo(regua, divida_centavos, politica)
    if alvo is None:
        return None
    return Oferta(1, divida_centavos - alvo, alvo)
