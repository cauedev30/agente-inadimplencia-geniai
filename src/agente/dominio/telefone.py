"""O telefone brasileiro em E.164: o formato em que o agente guarda e compara."""

import re


def normalizar_telefone(bruto: str) -> str | None:
    """Telefone brasileiro em E.164 (+55, DDD e número), ou None se não for um."""
    if bruto.strip().startswith("+") and not bruto.strip().startswith("+55"):
        return None
    digitos = re.sub(r"\D", "", bruto)
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        return None
    return f"+55{digitos}"


def variantes_telefone(bruto: str) -> frozenset[str]:
    """Os formatos em E.164 em que o mesmo telefone pode aparecer (A1).

    O celular brasileiro tem duas escritas: com o 9 na frente do número (o CTN as dá assim) e sem
    ele (o que o WhatsApp e o Chatwoot às vezes mandam). Devolve as duas. O fixo, que não tem 9,
    volta só ele mesmo. A entrada que não é telefone brasileiro não tem variante (conjunto vazio),
    então nada casa com ela.
    """
    e164 = normalizar_telefone(bruto)
    if e164 is None:
        return frozenset()
    ddd, numero = e164[3:5], e164[5:]
    if len(numero) == 9 and numero[0] == "9":
        return frozenset({e164, f"+55{ddd}{numero[1:]}"})
    # Os celulares sem o 9 começam em 6 a 9; o fixo começa em 2 a 5.
    if len(numero) == 8 and numero[0] in "6789":
        return frozenset({e164, f"+55{ddd}9{numero}"})
    return frozenset({e164})
