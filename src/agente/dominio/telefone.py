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
