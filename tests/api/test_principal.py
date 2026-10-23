import pytest
from fastapi.testclient import TestClient

from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.jev import JevComPrazo, JevFalso
from agente.integracoes.redator import RedatorFalso, RedatorGroq
from agente.tipos import Modo
from agente.webchat.link import ConfigWebchat

from ..conftest import RAIZ


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


def test_sem_a_chave_do_groq_o_redator_e_o_falso(caplog):
    deps = montar_dependencias(_config())
    assert isinstance(deps.redator, RedatorFalso)
    assert deps.redator.registrar is False
    assert "AGENTE_GROQ_API_KEY" in caplog.text


def test_com_a_chave_do_groq_o_redator_e_o_real():
    deps = montar_dependencias(_config(groq_api_key="chave-ficticia", groq_modelo="m-teste"))
    assert isinstance(deps.redator, RedatorGroq)
    assert deps.redator.modelo == "m-teste"
    deps.redator.fechar()


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
    config = _config(webchat_url_base="https://atendimento.exemplo.invalid", webchat_segredo="s")

    assert montar_dependencias(config).webchat == ConfigWebchat(
        "https://atendimento.exemplo.invalid", "s"
    )


@pytest.mark.parametrize("campos", [{}, {"webchat_url_base": "https://x.invalid"}])
def test_sem_dominio_ou_sem_segredo_o_link_continua_provisorio(campos):
    assert montar_dependencias(_config(**campos)).webchat is None
