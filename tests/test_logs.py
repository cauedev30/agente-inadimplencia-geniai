"""O log não leva o token do webchat, que é uma credencial de dias."""

import logging

from agente.logs import CortaTokenDoAcesso, configurar_logging, mascarar

TOKEN = "AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf012"


def test_o_log_de_acesso_do_uvicorn_perde_o_token():
    registro = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/atendimento/{TOKEN}?x=1", "1.1", 200),
        None,
    )

    assert CortaTokenDoAcesso().filter(registro)

    texto = registro.getMessage()
    assert TOKEN not in texto and 'GET /atendimento/***?x=1 HTTP/1.1" 200' in texto


def test_qualquer_log_mascara_o_token():
    assert mascarar(f"abriu /atendimento/{TOKEN} agora") == "abriu /atendimento/*** agora"


def test_o_filtro_do_acesso_entra_uma_vez_so_mesmo_configurando_de_novo():
    configurar_logging()
    configurar_logging()

    filtros = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(filtro, CortaTokenDoAcesso) for filtro in filtros) == 1
