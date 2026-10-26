import pytest
from fastapi.testclient import TestClient

from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.jev import JevComPrazo, JevFalso
from agente.integracoes.redator import RedatorFalso, RedatorOpenAI
from agente.integracoes.transcritor import TranscritorOpenAI
from agente.tipos import Modo
from agente.webchat.link import ConfigWebchat

from ..conftest import RAIZ

WEBCHAT_COMPLETO = {
    "webchat_url_base": "https://atendimento.exemplo.invalid",
    "webchat_segredo": "segredo-do-webchat-com-32-caracteres-ou-mais",
    "webchat_identidade_hmac": "chave-da-identidade",
    "webchat_website_token": "token-do-site",
    "chatwoot_url": "https://chat.invalid",
}


def _config(**campos):
    padrao = {
        "database_url": "sqlite://",
        "politica_path": RAIZ / "politica.toml",
        "webhook_segredo": "s3gredo",
    }
    return Config(**{**padrao, **campos})


def test_sem_chatwoot_configurado_usa_a_falsa_em_sombra():
    deps = montar_dependencias(_config())
    assert isinstance(deps.chatwoot, ChatwootFalso)
    assert deps.modo is Modo.SOMBRA


def test_com_chatwoot_configurado_usa_a_real():
    config = _config(chatwoot_url="https://chatwoot.invalid", chatwoot_conta=7, chatwoot_token="t")
    deps = montar_dependencias(config)
    assert isinstance(deps.chatwoot, ChatwootHttp)
    deps.chatwoot.fechar()


def test_modo_ativo_sem_chatwoot_real_e_recusado():
    with pytest.raises(ValueError, match="Chatwoot"):
        montar_dependencias(_config(modo="ativo"))


def test_ate_a_t10_o_jev_e_o_falso_com_o_prazo_da_politica(caplog):
    deps = montar_dependencias(_config())
    assert isinstance(deps.jev, JevComPrazo)
    assert isinstance(deps.jev.jev, JevFalso)
    assert deps.jev.timeout_ms == deps.politica.jev_timeout_ms
    assert deps.jev.jev.registrar is False
    assert "Jev" in caplog.text and "T10" in caplog.text


def test_sem_a_chave_do_llm_o_redator_e_o_falso(caplog):
    deps = montar_dependencias(_config())
    assert isinstance(deps.redator, RedatorFalso)
    assert deps.redator.registrar is False
    assert "AGENTE_LLM_API_KEY" in caplog.text


def test_com_a_chave_do_llm_o_redator_e_o_real():
    deps = montar_dependencias(_config(llm_api_key="chave-ficticia", llm_modelo="m-teste"))
    assert isinstance(deps.redator, RedatorOpenAI)
    assert deps.redator.modelo == "m-teste"
    deps.redator.fechar()


def test_o_redator_real_sai_com_o_luna_e_o_reasoning_effort_por_padrao():
    deps = montar_dependencias(_config(llm_api_key="chave-ficticia"))
    assert deps.redator.modelo == "gpt-6-luna"
    assert deps.redator.corpo_extra == {"reasoning_effort": "none"}
    deps.redator.fechar()


def test_a_troca_de_modelo_e_so_configuracao():
    config = _config(
        llm_api_key="chave-ficticia",
        llm_modelo="outro-modelo",
        llm_url="https://llm.invalid/v1",
        llm_corpo_extra={},
    )
    deps = montar_dependencias(config)
    assert deps.redator.nome == "openai:outro-modelo"
    assert deps.redator.corpo_extra == {}
    assert str(deps.redator._cliente.base_url) == "https://llm.invalid/v1/"
    deps.redator.fechar()


def test_sem_a_transcricao_configurada_o_audio_nao_e_transcrito(caplog):
    deps = montar_dependencias(_config())
    assert deps.transcritor is None
    assert "transcrição" in caplog.text.lower()


def test_com_a_transcricao_configurada_o_transcritor_e_o_real():
    config = _config(
        transcricao_url="https://transcricao.invalid/v1", transcricao_api_key="chave-ficticia"
    )
    deps = montar_dependencias(config)
    assert isinstance(deps.transcritor, TranscritorOpenAI)
    assert deps.transcritor.modelo == "gpt-4o-mini-transcribe"
    assert deps.transcritor.max_caracteres == deps.politica.transcricao_max_caracteres
    deps.transcritor.fechar()


def test_modo_ativo_sem_jev_real_e_recusado():
    config = _config(
        modo="ativo", chatwoot_url="https://chatwoot.invalid", chatwoot_conta=7, chatwoot_token="t"
    )
    with pytest.raises(ValueError, match="Jev"):
        montar_dependencias(config)


def test_app_de_producao_responde_a_saude():
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").json() == {"status": "ok"}


def test_com_dominio_e_segredo_o_handoff_leva_o_link_do_webchat():
    config = _config(**WEBCHAT_COMPLETO)

    assert montar_dependencias(config).webchat == ConfigWebchat(
        "https://atendimento.exemplo.invalid", WEBCHAT_COMPLETO["webchat_segredo"]
    )


@pytest.mark.parametrize(
    "faltando",
    [
        "webchat_url_base",
        "webchat_segredo",
        "webchat_identidade_hmac",
        "webchat_website_token",
        "chatwoot_url",
    ],
)
def test_com_a_pagina_desligada_o_link_continua_provisorio(faltando, caplog):
    campos = {**WEBCHAT_COMPLETO}
    del campos[faltando]

    assert montar_dependencias(_config(**campos)).webchat is None
    if faltando != "webchat_url_base":
        assert "webchat" in caplog.text.lower()


def test_sem_nada_do_webchat_o_link_continua_provisorio():
    assert montar_dependencias(_config()).webchat is None
