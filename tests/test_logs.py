"""O log não leva o segredo do webhook do Chatwoot, que vai no caminho (AUD-01)."""

import logging

from agente.logs import CortaTokenDoAcesso, MascaraDadosPessoais, configurar_logging, mascarar

SEGREDO = "abc123"


def test_o_log_de_acesso_do_uvicorn_perde_o_segredo_do_webhook():
    registro = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", f"/webhooks/chatwoot/{SEGREDO}", "1.1", 200),
        None,
    )

    assert CortaTokenDoAcesso().filter(registro)

    texto = registro.getMessage()
    assert SEGREDO not in texto
    assert 'POST /webhooks/chatwoot/*** HTTP/1.1" 200' in texto


def test_o_acesso_com_query_string_tambem_perde_o_segredo():
    registro = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", f"/webhooks/chatwoot/{SEGREDO}?x=1", "1.1", 401),
        None,
    )

    assert CortaTokenDoAcesso().filter(registro)

    texto = registro.getMessage()
    assert SEGREDO not in texto and "/webhooks/chatwoot/***?x=1" in texto


def test_qualquer_log_mascara_o_segredo_do_webhook():
    texto = mascarar(f"erro em POST /webhooks/chatwoot/{SEGREDO} HTTP/1.1")

    assert SEGREDO not in texto
    assert texto == "erro em POST /webhooks/chatwoot/*** HTTP/1.1"


def test_o_log_comum_perde_o_segredo_pelo_filtro_dos_dados_pessoais():
    registro = logging.LogRecord(
        "agente", logging.ERROR, __file__, 1, "falhou %s", (f"/webhooks/chatwoot/{SEGREDO}",), None
    )

    assert MascaraDadosPessoais().filter(registro)

    assert SEGREDO not in registro.getMessage()


def test_o_filtro_do_acesso_entra_uma_vez_so_mesmo_configurando_de_novo():
    configurar_logging()
    configurar_logging()

    filtros = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(filtro, CortaTokenDoAcesso) for filtro in filtros) == 1
