"""A identidade do cliente no widget do Chatwoot: um identificador opaco e o hash que o valida."""

import hashlib
import hmac
import re

_PADRAO = re.compile(r"cobranca-([0-9]{1,18})")


def identificador_do_chatwoot(cobranca_id: int) -> str:
    """O `identifier` do contato no widget: opaco, sem nome nem telefone."""
    return f"cobranca-{cobranca_id}"


def cobranca_do_identificador(identificador: object) -> int | None:
    """O id da cobrança de um `identifier` do widget, ou None se não for um nosso."""
    if not isinstance(identificador, str):
        return None
    achado = _PADRAO.fullmatch(identificador)
    return int(achado.group(1)) if achado else None


def hash_de_identidade(chave: str, identificador: str) -> str:
    """O `identifier_hash` da validação de identidade do Chatwoot: HMAC-SHA256 em hexadecimal."""
    return hmac.new(chave.encode(), identificador.encode(), hashlib.sha256).hexdigest()
