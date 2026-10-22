"""Logging do serviço. Nenhum CPF ou telefone completo chega ao log."""

import logging
import re
import sys

# Telefone em E.164: mantém o +55 e os 4 últimos dígitos.
_TELEFONE = re.compile(r"\+55(\d{6,7})(\d{4})\b")
_CPF_FORMATADO = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
# 11 dígitos soltos podem ser CPF ou telefone sem o +: mascara tudo.
_ONZE_DIGITOS = re.compile(r"\b\d{11}\b")


def mascarar(texto: str) -> str:
    texto = _TELEFONE.sub(lambda m: "+55" + "*" * len(m.group(1)) + m.group(2), texto)
    texto = _CPF_FORMATADO.sub("***.***.***-**", texto)
    return _ONZE_DIGITOS.sub("*" * 11, texto)


class MascaraDadosPessoais(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mascarar(record.getMessage())
        record.args = None
        return True


def configurar_logging(nivel: str = "INFO") -> None:
    # O console do Windows não usa UTF-8 por padrão e quebra os acentos.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    manipulador = logging.StreamHandler()
    manipulador.addFilter(MascaraDadosPessoais())
    manipulador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=nivel, handlers=[manipulador], force=True)
