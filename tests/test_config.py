import logging

import pytest
from pydantic import ValidationError

from agente.config import carregar_config
from agente.logs import MascaraDadosPessoais


def test_config_le_do_ambiente(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_MODO", "ativo")

    config = carregar_config()

    assert config.database_url == "sqlite:///teste.db"
    assert config.modo == "ativo"


def test_config_diz_qual_variavel_falta(monkeypatch):
    monkeypatch.delenv("AGENTE_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        carregar_config(_env_file=None)


def test_modo_invalido_e_recusado(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_MODO", "talvez")

    with pytest.raises(ValidationError, match="modo"):
        carregar_config(_env_file=None)


def _formatar(mensagem: str) -> str:
    registro = logging.LogRecord("agente", logging.INFO, __file__, 1, mensagem, None, None)
    MascaraDadosPessoais().filter(registro)
    return registro.getMessage()


def test_log_mascara_telefone():
    assert _formatar("mensagem de +5583991234567") == "mensagem de +55*******4567"


def test_log_mascara_cpf():
    assert _formatar("cpf 123.456.789-09 recebido") == "cpf ***.***.***-** recebido"
    assert _formatar("cpf 12345678909 recebido") == "cpf *********** recebido"


def test_log_nao_mascara_valor_em_centavos():
    assert _formatar("divida 10020 centavos") == "divida 10020 centavos"
