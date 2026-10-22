"""Régua (D25, D27) e dívida (D24) de uma cobrança."""

from agente.politica import Politica, Regua

_POR_MENSALIDADES = {
    1: Regua.R1,
    2: Regua.R2,
    3: Regua.R3,
    4: Regua.R4,
    5: Regua.R5,
    6: Regua.R6,
}


def _exigir_mensalidade(mensalidades_abertas: int) -> None:
    if mensalidades_abertas < 1:
        raise ValueError(
            f"a cobrança precisa de ao menos 1 mensalidade em aberto, veio {mensalidades_abertas}"
        )


def classificar_regua(
    mensalidades_abertas: int, regua_informada: Regua | None, politica: Politica
) -> Regua:
    """A régua informada pela lista vence (G5).

    A NR e a Prévia só valem quando a lista as informa (G10.1).
    """
    _exigir_mensalidade(mensalidades_abertas)
    if regua_informada is not None:
        return regua_informada
    return _POR_MENSALIDADES.get(mensalidades_abertas, politica.regua_acima_de_6)


def calcular_divida(mensalidades_abertas: int, politica: Politica) -> int:
    """A dívida é a mensalidade vezes as mensalidades em aberto, sem juros nem multa (D24)."""
    _exigir_mensalidade(mensalidades_abertas)
    return politica.mensalidade_centavos * mensalidades_abertas
