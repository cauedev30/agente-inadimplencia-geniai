"""Formata dinheiro em pt-BR. Os valores circulam sempre em centavos, em inteiro."""


def formatar_reais(centavos: int) -> str:
    """10020 vira "R$ 100,20"."""
    reais, resto = divmod(centavos, 100)
    milhares = f"{reais:,}".replace(",", ".")
    return f"R$ {milhares},{resto:02d}"
