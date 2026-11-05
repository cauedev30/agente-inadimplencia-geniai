import pytest
from fastapi.testclient import TestClient

import agente.api.app
from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.ctn import ClienteNaoEncontrado
from agente.integracoes.jev import JevComPrazo, JevFalso
from agente.integracoes.jev_typesafe import JevTypeSafe
from agente.integracoes.llm_turno import LLMTurnoOpenAI
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


def test_sem_a_chave_do_jev_o_falso_serve_com_ou_sem_a_liberacao():
    for liberada in (False, True):
        deps = montar_dependencias(_config(jev_dados_reais_liberados=liberada))
        assert isinstance(deps.jev.jev, JevFalso)


def test_com_a_chave_e_sem_a_liberacao_o_servico_nao_sobe():
    # A TypeSafe ainda não confirmou ZDR e No Training (D12): conversa real não vai ao Jev real.
    with pytest.raises(ValueError, match="AGENTE_JEV_DADOS_REAIS_LIBERADOS"):
        montar_dependencias(_config(jev_api_key="chave-ficticia"))


def test_com_a_chave_e_a_liberacao_o_jev_e_o_da_typesafe_com_75_por_cento_do_prazo():
    config = _config(
        jev_api_key="chave-ficticia", jev_dados_reais_liberados=True, jev_modelo="jev-1.14.0"
    )
    deps = montar_dependencias(config)
    assert isinstance(deps.jev, JevComPrazo)
    assert isinstance(deps.jev.jev, JevTypeSafe)
    assert deps.jev.origem == "typesafe:jev-1.14.0"
    assert deps.jev.timeout_ms == deps.politica.jev_timeout_ms
    assert deps.jev.jev._cliente.timeout.read == deps.politica.jev_timeout_ms * 0.75 / 1000
    deps.jev.jev.fechar()


def test_o_chatwoot_real_e_fechado_se_o_jev_for_recusado(monkeypatch):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append(self))
    config = _config(
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        jev_api_key="chave-ficticia",
    )
    with pytest.raises(ValueError, match="AGENTE_JEV"):
        montar_dependencias(config)
    assert len(fechados) == 1


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


def _politica_com_liberacao(tmp_path, liberacao: str):
    """Cópia do politica.toml da raiz, trocando só a linha `liberacao = ...`."""
    linhas = (RAIZ / "politica.toml").read_text(encoding="utf-8").splitlines()
    assert sum(linha.startswith("liberacao = ") for linha in linhas) == 1
    texto = "\n".join(
        f"liberacao = {liberacao}" if linha.startswith("liberacao = ") else linha
        for linha in linhas
    )
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto, encoding="utf-8")
    return caminho


PILOTO = '{ modo = "piloto", telefones = ["+5583900000001"] }'
PERCENTUAL = '{ modo = "percentual", percentual = 10 }'


def _config_ativo(tmp_path, liberacao: str, **campos):
    return _config(
        modo="ativo",
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        politica_path=_politica_com_liberacao(tmp_path, liberacao),
        **campos,
    )


def test_modo_ativo_com_o_jev_real_e_o_piloto_sobe(tmp_path):
    config = _config_ativo(
        tmp_path, PILOTO, jev_api_key="chave-ficticia", jev_dados_reais_liberados=True
    )
    deps = montar_dependencias(config)
    try:
        assert deps.modo is Modo.ATIVO
        assert isinstance(deps.chatwoot, ChatwootHttp)
        assert isinstance(deps.jev, JevComPrazo)
        assert isinstance(deps.jev.jev, JevTypeSafe)
        # Até a T9 o CTN é o falso; no ativo (piloto) ele aceita a matrícula da carteira.
        assert deps.ctn.consultar_cliente("DEMO-0001").matricula == "DEMO-0001"
    finally:
        deps.chatwoot.fechar()
        deps.jev.jev.fechar()


def test_modo_sombra_segue_com_o_ctn_falso_sem_clientes():
    deps = montar_dependencias(_config())
    with pytest.raises(ClienteNaoEncontrado):
        deps.ctn.consultar_cliente("DEMO-0001")


@pytest.mark.parametrize("liberacao", [PERCENTUAL, '"sombra"'])
def test_modo_ativo_so_roda_com_a_liberacao_em_piloto_enquanto_pix_e_ctn_forem_falsos(
    tmp_path, liberacao
):
    config = _config_ativo(
        tmp_path, liberacao, jev_api_key="chave-ficticia", jev_dados_reais_liberados=True
    )
    with pytest.raises(ValueError, match="piloto"):
        montar_dependencias(config)


def test_modo_ativo_com_a_chave_do_jev_sem_a_liberacao_da_zdr_e_recusado(tmp_path):
    config = _config_ativo(tmp_path, PILOTO, jev_api_key="chave-ficticia")
    with pytest.raises(ValueError, match="AGENTE_JEV_DADOS_REAIS_LIBERADOS"):
        montar_dependencias(config)


@pytest.mark.parametrize(
    ("liberacao", "campos", "trecho"),
    [
        (PILOTO, {}, "Jev"),
        (
            PERCENTUAL,
            {"jev_api_key": "chave-ficticia", "jev_dados_reais_liberados": True},
            "piloto",
        ),
        (PILOTO, {"jev_api_key": "chave-ficticia"}, "AGENTE_JEV"),
    ],
)
def test_os_erros_do_modo_ativo_fecham_o_chatwoot_real(
    monkeypatch, tmp_path, liberacao, campos, trecho
):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append(self))
    with pytest.raises(ValueError, match=trecho):
        montar_dependencias(_config_ativo(tmp_path, liberacao, **campos))
    assert len(fechados) == 1


def test_app_de_producao_responde_a_saude():
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").json() == {"status": "ok"}


def test_o_encerramento_do_servico_fecha_o_cliente_do_jev_real(monkeypatch):
    fechados = []
    monkeypatch.setattr(JevTypeSafe, "fechar", lambda self: fechados.append(self))
    config = _config(jev_api_key="chave-ficticia", jev_dados_reais_liberados=True)

    with TestClient(montar_app(config, iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").status_code == 200
        assert fechados == []

    assert len(fechados) == 1


def test_o_encerramento_com_o_jev_falso_segue_sem_erro():
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").status_code == 200


def test_falha_ao_fechar_o_jev_nao_impede_o_encerramento(monkeypatch):
    def estoura(self):
        raise RuntimeError("falha ao fechar")

    registros = []
    monkeypatch.setattr(JevTypeSafe, "fechar", estoura)
    # Sem capsys nem caplog: o montar_app reconfigura o log e deixaria um manipulador preso à saída
    # temporária do teste.
    monkeypatch.setattr(
        agente.api.app.log, "exception", lambda mensagem: registros.append(mensagem)
    )
    config = _config(jev_api_key="chave-ficticia", jev_dados_reais_liberados=True)

    with TestClient(montar_app(config, iniciar_trabalhador=False)):
        pass

    assert any("fechar o cliente do Jev" in mensagem for mensagem in registros)


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


# O turno de uma chamada (D47, E1.6)


def test_com_o_pipeline_nao_ha_llm_turno():
    deps = montar_dependencias(_config(llm_api_key="chave-ficticia"))

    assert deps.llm_turno is None
    deps.redator.fechar()


def test_uma_chamada_sem_a_chave_do_llm_nao_sobe():
    with pytest.raises(ValueError, match=r"AGENTE_TURNO=uma_chamada exige o AGENTE_LLM_API_KEY"):
        montar_dependencias(_config(turno="uma_chamada"))


def test_uma_chamada_com_a_chave_monta_o_llm_do_turno_com_o_luna(caplog):
    caplog.set_level("INFO")
    deps = montar_dependencias(_config(turno="uma_chamada", llm_api_key="chave-ficticia"))
    try:
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        assert deps.llm_turno.modelo == "gpt-6-luna"
        assert deps.llm_turno.corpo_extra == {"reasoning_effort": "none"}
        assert "turno de uma chamada (D47)" in caplog.text
        assert "chave-ficticia" not in caplog.text
    finally:
        deps.llm_turno.fechar()
        deps.redator.fechar()


def test_uma_chamada_usa_a_url_e_o_modelo_da_configuracao():
    config = _config(
        turno="uma_chamada",
        llm_api_key="chave-ficticia",
        llm_modelo="outro-modelo",
        llm_url="https://llm.invalid/v1",
        llm_corpo_extra={},
    )
    deps = montar_dependencias(config)
    try:
        assert deps.llm_turno.nome == "openai:outro-modelo"
        assert deps.llm_turno.corpo_extra == {}
        assert str(deps.llm_turno._cliente.base_url) == "https://llm.invalid/v1/"
    finally:
        deps.llm_turno.fechar()
        deps.redator.fechar()


def test_uma_chamada_com_o_modo_ativo_sobe_com_a_chave_do_luna_sem_a_do_jev_no_piloto(tmp_path):
    config = _config_ativo(tmp_path, PILOTO, turno="uma_chamada", llm_api_key="chave-ficticia")
    deps = montar_dependencias(config)
    try:
        assert deps.modo is Modo.ATIVO
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        # Sem a chave do Jev, o Jev é o falso, e o turno novo nunca o chama.
        assert isinstance(deps.jev.jev, JevFalso)
    finally:
        deps.chatwoot.fechar()
        deps.llm_turno.fechar()
        deps.redator.fechar()


def test_uma_chamada_com_o_modo_ativo_sem_a_chave_do_luna_e_recusada(tmp_path):
    config = _config_ativo(tmp_path, PILOTO, turno="uma_chamada")
    with pytest.raises(ValueError, match="o modo ativo com AGENTE_TURNO=uma_chamada exige"):
        montar_dependencias(config)


def test_uma_chamada_com_o_modo_ativo_fora_do_piloto_e_recusada(tmp_path):
    config = _config_ativo(tmp_path, PERCENTUAL, turno="uma_chamada", llm_api_key="chave-ficticia")
    with pytest.raises(ValueError, match="piloto"):
        montar_dependencias(config)


def test_o_pipeline_com_o_modo_ativo_sem_a_chave_do_jev_segue_recusado(tmp_path):
    config = _config_ativo(tmp_path, PILOTO, llm_api_key="chave-ficticia")
    with pytest.raises(ValueError, match="o modo ativo exige o Jev real"):
        montar_dependencias(config)


def test_a_recusa_do_modo_ativo_no_turno_novo_fecha_o_chatwoot_real(monkeypatch, tmp_path):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append(self))
    with pytest.raises(ValueError, match="uma_chamada"):
        montar_dependencias(_config_ativo(tmp_path, PILOTO, turno="uma_chamada"))
    assert len(fechados) == 1
