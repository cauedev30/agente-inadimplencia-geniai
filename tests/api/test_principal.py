import pytest
from fastapi.testclient import TestClient

from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.tipos import Modo

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


def test_app_de_producao_responde_a_saude():
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").json() == {"status": "ok"}
