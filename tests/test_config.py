import logging
import os
from pathlib import Path

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


@pytest.mark.parametrize(
    "variavel", ["PIX_WEBHOOK_SEGREDO", "CTN_WEBHOOK_SEGREDO", "WEBHOOK_SEGREDO"]
)
def test_segredo_vazio_vira_desligado(monkeypatch, variavel):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv(f"AGENTE_{variavel}", "  ")

    config = carregar_config(_env_file=None)

    assert getattr(config, variavel.lower()) is None


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


def test_config_le_a_chave_e_o_modelo_do_llm(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_LLM_API_KEY", "chave-ficticia")

    config = carregar_config(_env_file=None)

    assert config.llm_api_key == "chave-ficticia"
    assert config.llm_modelo == "gpt-6-luna"
    assert config.llm_url == "https://api.openai.com/v1"
    assert config.llm_corpo_extra == {"reasoning_effort": "none"}


def test_config_le_o_corpo_extra_do_llm_como_json(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_LLM_CORPO_EXTRA", '{"reasoning_effort": "low"}')

    assert carregar_config(_env_file=None).llm_corpo_extra == {"reasoning_effort": "low"}


def test_o_segredo_curto_do_webchat_e_recusado():
    with pytest.raises(ValidationError, match="32"):
        carregar_config(database_url="sqlite://", webchat_segredo="curto")
    carregar_config(database_url="sqlite://", webchat_segredo="x" * 32)


def test_webchat_ligado_exige_todos_os_segredos_e_o_chatwoot():
    completo = {
        "webchat_segredo": "x" * 32,
        "webchat_identidade_hmac": "h",
        "webchat_website_token": "t",
        "chatwoot_url": "https://chat.invalid",
    }
    assert carregar_config(database_url="sqlite://", **completo).webchat_ligado
    for faltando in completo:
        parcial = {k: v for k, v in completo.items() if k != faltando}
        assert not carregar_config(database_url="sqlite://", **parcial).webchat_ligado


def test_segredos_do_webchat_vazios_valem_como_ausentes():
    config = carregar_config(
        database_url="sqlite://",
        webchat_segredo="  ",
        webchat_identidade_hmac="",
        webchat_website_token="",
        webchat_url_base="",
    )

    assert config.webchat_segredo is None
    assert config.webchat_identidade_hmac is None
    assert config.webchat_website_token is None
    assert config.webchat_url_base is None


def test_sem_nenhuma_variavel_da_transcricao_ela_fica_desligada():
    config = carregar_config(database_url="sqlite://")

    assert not config.transcricao_ligada


def test_com_a_url_e_a_chave_a_transcricao_liga_com_o_modelo_padrao():
    config = carregar_config(
        database_url="sqlite://",
        transcricao_url="https://transcricao.invalid/v1",
        transcricao_api_key="chave-ficticia",
    )

    assert config.transcricao_ligada
    assert config.transcricao_modelo_usado == "gpt-4o-mini-transcribe"


def test_o_modelo_da_transcricao_pode_ser_trocado():
    config = carregar_config(
        database_url="sqlite://",
        transcricao_url="https://transcricao.invalid/v1",
        transcricao_api_key="chave-ficticia",
        transcricao_modelo="outro-modelo",
    )

    assert config.transcricao_modelo_usado == "outro-modelo"


@pytest.mark.parametrize(
    ("dadas", "faltam"),
    [
        ({"transcricao_url": "https://t.invalid/v1"}, ["AGENTE_TRANSCRICAO_API_KEY"]),
        ({"transcricao_api_key": "chave-ficticia"}, ["AGENTE_TRANSCRICAO_URL"]),
        (
            {"transcricao_modelo": "m"},
            ["AGENTE_TRANSCRICAO_URL", "AGENTE_TRANSCRICAO_API_KEY"],
        ),
    ],
)
def test_a_transcricao_pela_metade_e_recusada_dizendo_o_que_falta(dadas, faltam):
    with pytest.raises(ValidationError) as erro:
        carregar_config(database_url="sqlite://", **dadas)

    for nome in faltam:
        assert nome in str(erro.value)
    # O erro cita os nomes, nunca os valores.
    assert "chave-ficticia" not in str(erro.value)


def test_a_transcricao_vazia_vale_como_desligada():
    config = carregar_config(
        database_url="sqlite://",
        transcricao_url="",
        transcricao_api_key="  ",
        transcricao_modelo="",
    )

    assert not config.transcricao_ligada


def test_o_env_example_copiado_como_esta_sobe_com_tudo_desligado(monkeypatch):
    for variavel in list(os.environ):
        if variavel.startswith("AGENTE_"):
            monkeypatch.delenv(variavel)

    config = carregar_config(_env_file=Path(__file__).parents[1] / ".env.example")

    assert not config.transcricao_ligada and not config.webchat_ligado
    assert config.ctn_api_key is None and config.llm_dados_reais_liberados is False
    assert config.chatwoot_conta is None and config.chatwoot_inbox is None
    assert config.chatwoot_time_humano is None and config.webchat_inbox is None


def test_sem_variavel_do_ctn_ele_fica_desligado_e_a_liberacao_do_llm_fechada(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")

    config = carregar_config(_env_file=None)

    assert config.ctn_api_key is None
    assert config.ctn_url == "https://api.eficaz-group.com"
    assert config.ctn_unidade == "jp"
    assert config.llm_dados_reais_liberados is False
    assert config.ctn_real_ligado is False


def test_config_le_as_variaveis_do_ctn(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_CTN_API_KEY", "  chave-ficticia  ")
    monkeypatch.setenv("AGENTE_CTN_URL", "https://eficaz.invalid")
    monkeypatch.setenv("AGENTE_CTN_UNIDADE", "sul")
    monkeypatch.setenv("AGENTE_LLM_DADOS_REAIS_LIBERADOS", "true")

    config = carregar_config(_env_file=None)

    assert config.ctn_api_key == "chave-ficticia"
    assert config.ctn_url == "https://eficaz.invalid"
    assert config.ctn_unidade == "sul"
    assert config.llm_dados_reais_liberados is True
    assert config.ctn_real_ligado is True


@pytest.mark.parametrize("vazio", ["", "  "])
def test_variaveis_do_ctn_vazias_valem_como_ausentes(monkeypatch, vazio):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    for variavel in ("API_KEY", "URL", "UNIDADE"):
        monkeypatch.setenv(f"AGENTE_CTN_{variavel}", vazio)
    monkeypatch.setenv("AGENTE_LLM_DADOS_REAIS_LIBERADOS", vazio)

    config = carregar_config(_env_file=None)

    assert config.ctn_api_key is None
    assert config.ctn_url == "https://api.eficaz-group.com"
    assert config.ctn_unidade == "jp"
    assert config.llm_dados_reais_liberados is False


def test_a_chave_do_ctn_nao_aparece_no_repr_da_config(monkeypatch):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_CTN_API_KEY", "eg_SEGREDO_INVENTADO")

    config = carregar_config(_env_file=None)

    assert "eg_SEGREDO_INVENTADO" not in repr(config)
    assert "eg_SEGREDO_INVENTADO" not in str(config)


@pytest.mark.parametrize("valor", ["talvez", " true "])
def test_liberacao_do_llm_ambigua_falha_fechada(monkeypatch, valor):
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///teste.db")
    monkeypatch.setenv("AGENTE_LLM_DADOS_REAIS_LIBERADOS", valor)

    with pytest.raises(ValidationError):
        carregar_config(_env_file=None)
