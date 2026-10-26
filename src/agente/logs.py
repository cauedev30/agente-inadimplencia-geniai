"""Logging do serviço. Nenhum CPF ou telefone completo chega ao log."""

import logging
import re
import sys

# Telefone em E.164: mantém o +55 e os 4 últimos dígitos.
_TELEFONE = re.compile(r"\+55(\d{6,7})(\d{4})\b")
_CPF_FORMATADO = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
# 11 dígitos soltos podem ser CPF ou telefone sem o +: mascara tudo.
_ONZE_DIGITOS = re.compile(r"\b\d{11}\b")
# O token do link do webchat é uma credencial de dias: não vai ao log de acesso.
_TOKEN_WEBCHAT = re.compile(r"/atendimento/[^/\s?#\"']+")


def cortar_token_webchat(texto: str) -> str:
    return _TOKEN_WEBCHAT.sub("/atendimento/***", texto)


def mascarar(texto: str) -> str:
    texto = cortar_token_webchat(texto)
    texto = _TELEFONE.sub(lambda m: "+55" + "*" * len(m.group(1)) + m.group(2), texto)
    texto = _CPF_FORMATADO.sub("***.***.***-**", texto)
    return _ONZE_DIGITOS.sub("*" * 11, texto)


class MascaraDadosPessoais(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mascarar(record.getMessage())
        record.args = None
        return True


class CortaTokenDoAcesso(logging.Filter):
    """O log de acesso do uvicorn leva o caminho em `args`: troca o token por `***`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                cortar_token_webchat(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def configurar_logging(nivel: str = "INFO") -> None:
    # O console do Windows não usa UTF-8 por padrão e quebra os acentos.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    manipulador = logging.StreamHandler()
    manipulador.addFilter(MascaraDadosPessoais())
    manipulador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=nivel, handlers=[manipulador], force=True)
    acesso = logging.getLogger("uvicorn.access")
    if not any(isinstance(filtro, CortaTokenDoAcesso) for filtro in acesso.filters):
        acesso.addFilter(CortaTokenDoAcesso())
