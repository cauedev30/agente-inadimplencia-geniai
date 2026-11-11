from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.db.modelos import Base, Cliente, Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.ctn import ClienteNaoEncontrado, CTNComposto, CTNFalso
from agente.integracoes.ctn_eficaz import CTNEficaz
from agente.integracoes.ctn_teste import CTNDeCasos, CTNEscritaDeTeste, RoteadorCTN
from agente.integracoes.llm_turno import LLMTurnoOpenAI
from agente.integracoes.pix import PixDeTeste
from agente.integracoes.transcritor import TranscritorOpenAI
from agente.politica import Regua
from agente.tipos import Modo
from agente.webchat.link import ConfigWebchat

from ..conftest import RAIZ, TESTADOR_1, escrever_politica_de_teste

WEBCHAT_COMPLETO = {
    "webchat_url_base": "https://atendimento.exemplo.invalid",
    "webchat_segredo": "segredo-do-webchat-com-32-caracteres-ou-mais",
    "webchat_identidade_hmac": "chave-da-identidade",
    "webchat_website_token": "token-do-site",
    "chatwoot_url": "https://chat.invalid",
}
CHAVE_LUNA = "chave-do-luna"


def _config(**campos):
    # O serviço só sobe com a chave do Luna (D59): o padrão dos testes a traz.
    padrao = {
        "database_url": "sqlite://",
        "politica_path": RAIZ / "politica.toml",
        "webhook_segredo": "s3gredo",
        "llm_api_key": CHAVE_LUNA,
    }
    return Config(**{**padrao, **campos})


def _fecha_o_luna(deps):
    deps.llm_turno.fechar()


def test_sem_chatwoot_configurado_usa_a_falsa_em_sombra():
    deps = montar_dependencias(_config())
    try:
        assert isinstance(deps.chatwoot, ChatwootFalso)
        assert deps.modo is Modo.SOMBRA
    finally:
        _fecha_o_luna(deps)


def test_com_chatwoot_configurado_usa_a_real():
    config = _config(
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        llm_dados_reais_liberados=True,
    )
    deps = montar_dependencias(config)
    assert isinstance(deps.chatwoot, ChatwootHttp)
    deps.chatwoot.fechar()
    _fecha_o_luna(deps)


def test_modo_ativo_sem_chatwoot_real_e_recusado():
    with pytest.raises(ValueError, match="Chatwoot"):
        montar_dependencias(_config(modo="ativo"))


def test_sem_a_chave_do_llm_o_servico_nao_sobe():
    with pytest.raises(ValueError, match="AGENTE_LLM_API_KEY"):
        montar_dependencias(_config(llm_api_key=None))


def test_o_chatwoot_real_e_fechado_se_a_chave_do_llm_faltar(monkeypatch):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append(self))
    config = _config(
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        llm_api_key=None,
    )
    with pytest.raises(ValueError, match="AGENTE_LLM_API_KEY"):
        montar_dependencias(config)
    assert len(fechados) == 1


def test_com_a_chave_do_llm_o_turno_e_o_luna_real(caplog):
    caplog.set_level("INFO")
    deps = montar_dependencias(_config(llm_modelo="m-teste"))
    try:
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        assert deps.llm_turno.modelo == "m-teste"
        assert CHAVE_LUNA not in caplog.text
    finally:
        _fecha_o_luna(deps)


def test_o_luna_sai_com_o_gpt_6_luna_e_o_reasoning_effort_por_padrao():
    deps = montar_dependencias(_config())
    try:
        assert deps.llm_turno.modelo == "gpt-6-luna"
        assert deps.llm_turno.corpo_extra == {"reasoning_effort": "none"}
    finally:
        _fecha_o_luna(deps)


def test_a_troca_de_modelo_e_so_configuracao():
    config = _config(
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
        _fecha_o_luna(deps)


def test_sem_a_transcricao_configurada_o_audio_nao_e_transcrito(caplog):
    deps = montar_dependencias(_config())
    assert deps.transcritor is None
    assert "transcrição" in caplog.text.lower()
    _fecha_o_luna(deps)


def test_com_a_transcricao_configurada_o_transcritor_e_o_real():
    config = _config(
        transcricao_url="https://transcricao.invalid/v1", transcricao_api_key="chave-ficticia"
    )
    deps = montar_dependencias(config)
    assert isinstance(deps.transcritor, TranscritorOpenAI)
    assert deps.transcritor.modelo == "gpt-4o-mini-transcribe"
    assert deps.transcritor.max_caracteres == deps.politica.transcricao_max_caracteres
    deps.transcritor.fechar()
    _fecha_o_luna(deps)


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
    # O Chatwoot real com o Luna real pede a liberação da OpenAI (M2).
    return _config(
        modo="ativo",
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        politica_path=_politica_com_liberacao(tmp_path, liberacao),
        **{"llm_dados_reais_liberados": True, **campos},
    )


def test_modo_ativo_com_o_luna_e_o_piloto_sobe(tmp_path):
    deps = montar_dependencias(_config_ativo(tmp_path, PILOTO))
    try:
        assert deps.modo is Modo.ATIVO
        assert isinstance(deps.chatwoot, ChatwootHttp)
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        # Até a T9 o CTN é o falso; no ativo (piloto) ele aceita a matrícula da carteira.
        assert deps.ctn.consultar_cliente("DEMO-0001").matricula == "DEMO-0001"
    finally:
        deps.chatwoot.fechar()
        _fecha_o_luna(deps)


def test_modo_sombra_segue_com_o_ctn_falso_sem_clientes():
    deps = montar_dependencias(_config())
    with pytest.raises(ClienteNaoEncontrado):
        deps.ctn.consultar_cliente("DEMO-0001")
    _fecha_o_luna(deps)


@pytest.mark.parametrize("liberacao", [PERCENTUAL, '"sombra"'])
def test_modo_ativo_so_roda_com_a_liberacao_em_piloto_enquanto_pix_e_ctn_forem_falsos(
    tmp_path, liberacao
):
    with pytest.raises(ValueError, match="piloto"):
        montar_dependencias(_config_ativo(tmp_path, liberacao))


def test_modo_ativo_sem_a_chave_do_luna_e_recusado(tmp_path):
    with pytest.raises(ValueError, match="AGENTE_LLM_API_KEY"):
        montar_dependencias(_config_ativo(tmp_path, PILOTO, llm_api_key=None))


@pytest.mark.parametrize(
    ("liberacao", "campos", "trecho"),
    [
        (PILOTO, {"llm_api_key": None}, "AGENTE_LLM_API_KEY"),
        (PERCENTUAL, {}, "piloto"),
        (PILOTO, {"llm_dados_reais_liberados": False}, "AGENTE_LLM_DADOS_REAIS_LIBERADOS"),
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


# ---- o CTN real (EFICAZ) e a trava do Luna (M2) ----

CHAVE_CTN = "eg_SEGREDO_INVENTADO_123"


def test_sem_a_chave_do_ctn_o_falso_de_hoje_serve():
    deps = montar_dependencias(_config())
    assert isinstance(deps.ctn, CTNFalso)
    _fecha_o_luna(deps)


def test_com_a_chave_do_ctn_o_ctn_e_o_composto_com_a_eficaz_na_leitura_e_o_falso_na_escrita():
    deps = montar_dependencias(
        _config(ctn_api_key=CHAVE_CTN, ctn_unidade="jp", llm_dados_reais_liberados=True)
    )
    try:
        assert isinstance(deps.ctn, CTNComposto)
        assert isinstance(deps.ctn.leitura, CTNEficaz)
        assert isinstance(deps.ctn.escrita, CTNFalso)
    finally:
        deps.ctn.fechar()


CHATWOOT_REAL = {
    "chatwoot_url": "https://chatwoot.invalid",
    "chatwoot_conta": 7,
    "chatwoot_token": "token-do-chatwoot",
}
TRANSCRICAO = {
    "transcricao_url": "https://transcricao.invalid/v1",
    "transcricao_api_key": "chave-da-transcricao",
}
CTN_REAL = {"ctn_api_key": CHAVE_CTN}


def _fecha(deps):
    if isinstance(deps.chatwoot, ChatwootHttp):
        deps.chatwoot.fechar()
    if hasattr(deps.ctn, "fechar"):
        deps.ctn.fechar()
    _fecha_o_luna(deps)


# (fonte real, destino externo a mais, o que a mensagem de recusa tem de nomear). O Luna é sempre
# um destino: o serviço não sobe sem a chave dele (D59).
RECUSADAS = {
    "ctn": (CTN_REAL, {}, ["AGENTE_CTN_API_KEY", "AGENTE_LLM_API_KEY"]),
    "chatwoot": (CHATWOOT_REAL, {}, ["AGENTE_CHATWOOT_*", "AGENTE_LLM_API_KEY"]),
    "ctn+transcricao": (
        CTN_REAL,
        TRANSCRICAO,
        ["AGENTE_CTN_API_KEY", "AGENTE_LLM_API_KEY", "AGENTE_TRANSCRICAO_*"],
    ),
    "chatwoot+transcricao": (
        CHATWOOT_REAL,
        TRANSCRICAO,
        ["AGENTE_CHATWOOT_*", "AGENTE_LLM_API_KEY", "AGENTE_TRANSCRICAO_*"],
    ),
}


@pytest.mark.parametrize("combinacao", RECUSADAS)
def test_fonte_real_com_destino_externo_sem_a_liberacao_o_servico_nao_sobe(combinacao):
    fonte, destino, nomes = RECUSADAS[combinacao]
    with pytest.raises(ValueError, match="AGENTE_LLM_DADOS_REAIS_LIBERADOS") as erro:
        montar_dependencias(_config(**fonte, **destino))
    mensagem = str(erro.value)
    assert all(nome in mensagem for nome in nomes)
    for segredo in (
        CHAVE_CTN,
        CHAVE_LUNA,
        "chave-da-transcricao",
        "token-do-chatwoot",
        "chatwoot.invalid",
        "transcricao.invalid",
    ):
        assert segredo not in mensagem


def test_a_mensagem_da_recusa_so_nomeia_a_combinacao_que_disparou():
    with pytest.raises(ValueError) as erro:
        montar_dependencias(_config(**CTN_REAL, **TRANSCRICAO))
    assert "AGENTE_CHATWOOT" not in str(erro.value)


def test_a_mensagem_nomeia_todas_as_fontes_e_todos_os_destinos_que_dispararam():
    with pytest.raises(ValueError) as erro:
        montar_dependencias(_config(**CTN_REAL, **CHATWOOT_REAL, **TRANSCRICAO))
    for nome in ("AGENTE_CTN_API_KEY", "AGENTE_CHATWOOT_*", "AGENTE_LLM_API_KEY"):
        assert nome in str(erro.value)
    assert "AGENTE_TRANSCRICAO_*" in str(erro.value)


@pytest.mark.parametrize("combinacao", RECUSADAS)
def test_fonte_real_com_destino_externo_e_a_liberacao_o_servico_sobe(combinacao):
    fonte, destino, _ = RECUSADAS[combinacao]
    deps = montar_dependencias(_config(**fonte, **destino, llm_dados_reais_liberados=True))
    _fecha(deps)


@pytest.mark.parametrize(
    "campos",
    [
        pytest.param({}, id="so-luna"),
        pytest.param(TRANSCRICAO, id="so-destinos"),
    ],
)
def test_sem_fonte_real_sobe_sem_a_liberacao(campos):
    deps = montar_dependencias(_config(**campos))
    _fecha(deps)


def test_chatwoot_pela_metade_nao_e_fonte_real():
    # O serviço só usa o Chatwoot real com a URL, a conta e o token (montar_dependencias).
    deps = montar_dependencias(_config(chatwoot_url="https://chatwoot.invalid"))
    assert isinstance(deps.chatwoot, ChatwootFalso)
    _fecha_o_luna(deps)


def test_ctn_real_com_o_luna_real_sem_a_liberacao_o_servico_nao_sobe():
    config = _config(ctn_api_key=CHAVE_CTN)
    with pytest.raises(ValueError, match="AGENTE_LLM_DADOS_REAIS_LIBERADOS") as erro:
        montar_dependencias(config)
    assert CHAVE_CTN not in str(erro.value) and CHAVE_LUNA not in str(erro.value)


def test_ctn_real_com_o_luna_real_e_a_liberacao_o_servico_sobe():
    config = _config(ctn_api_key=CHAVE_CTN, llm_dados_reais_liberados=True)
    deps = montar_dependencias(config)
    try:
        assert isinstance(deps.ctn, CTNComposto)
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
    finally:
        deps.ctn.fechar()
        _fecha_o_luna(deps)


def test_luna_real_sem_o_ctn_real_sobe_sem_a_liberacao():
    deps = montar_dependencias(_config())
    assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
    _fecha_o_luna(deps)


def test_a_recusa_do_luna_fecha_o_chatwoot_e_o_ctn_real(monkeypatch):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append("chatwoot"))
    monkeypatch.setattr(CTNEficaz, "fechar", lambda self: fechados.append("ctn"))
    config = _config(
        chatwoot_url="https://chatwoot.invalid",
        chatwoot_conta=7,
        chatwoot_token="t",
        ctn_api_key=CHAVE_CTN,
    )
    with pytest.raises(ValueError, match="AGENTE_LLM"):
        montar_dependencias(config)
    assert sorted(fechados) == ["chatwoot"]


def test_o_encerramento_do_servico_fecha_o_cliente_do_ctn_real(monkeypatch):
    fechados = []
    monkeypatch.setattr(CTNEficaz, "fechar", lambda self: fechados.append(self))

    config = _config(ctn_api_key=CHAVE_CTN, llm_dados_reais_liberados=True)
    with TestClient(montar_app(config, iniciar_trabalhador=False)):
        assert fechados == []

    assert len(fechados) == 1


# ---- o modo teste (D56): o roteador de casos e a trava de subida ----


def _config_de_teste(tmp_path, **campos):
    return _config(politica_path=escrever_politica_de_teste(tmp_path), **campos)


def test_sem_o_bloco_teste_nao_ha_casos_e_o_ctn_segue_igual():
    deps = montar_dependencias(_config())
    assert deps.casos is None
    assert isinstance(deps.ctn, CTNFalso)
    assert not isinstance(deps.pix, PixDeTeste)
    _fecha_o_luna(deps)


def test_o_modo_teste_sem_a_chave_do_ctn_usa_o_roteador_com_o_real_falso_e_vazio(tmp_path):
    deps = montar_dependencias(_config_de_teste(tmp_path))
    assert isinstance(deps.casos, CTNDeCasos)
    assert isinstance(deps.ctn, CTNComposto)
    assert isinstance(deps.ctn.leitura, RoteadorCTN)
    assert deps.ctn.leitura.simulados is deps.casos
    assert isinstance(deps.ctn.leitura.real, CTNFalso)
    assert isinstance(deps.ctn.escrita, CTNEscritaDeTeste)
    assert isinstance(deps.pix, PixDeTeste)
    assert deps.conferir_no_contato is True
    # A carteira real vazia: a matrícula real não existe; a simulada responde pelo cardápio.
    with pytest.raises(ClienteNaoEncontrado):
        deps.ctn.consultar_cliente("REAL-1")
    deps.casos.ativar("SIM-3-7777", "+5583988887777")
    assert deps.ctn.consultar_cliente("SIM-3-7777").nome == "Carla"
    assert len(deps.ctn.mensalidades_em_aberto("SIM-3-7777")) == 4
    assert deps.ctn.gerar_link_pagamento("SIM-3-7777", 13360, 1).url.startswith("[TESTE]")


def test_o_modo_teste_com_a_chave_do_ctn_roteia_para_a_eficaz(tmp_path):
    deps = montar_dependencias(
        _config_de_teste(tmp_path, ctn_api_key=CHAVE_CTN, llm_dados_reais_liberados=True)
    )
    try:
        assert isinstance(deps.ctn.leitura, RoteadorCTN)
        assert isinstance(deps.ctn.leitura.real, CTNEficaz)
        assert deps.ctn.leitura.simulados is deps.casos
        assert isinstance(deps.ctn.escrita, CTNEscritaDeTeste)
    finally:
        deps.ctn.fechar()


def test_o_casos_usa_a_mensalidade_da_politica_e_o_dia_do_fuso_da_janela(tmp_path):
    deps = montar_dependencias(_config_de_teste(tmp_path))
    deps.casos.ativar("SIM-1-7777", "+5583988887777")
    abertas = deps.casos.mensalidades_em_aberto("SIM-1-7777")
    assert {a.valor_centavos for a in abertas} == {deps.politica.mensalidade_centavos}
    agora = datetime.now(deps.politica.janela.zona)
    mes_corrente = f"{agora.year:04d}/{agora.month:02d}"
    assert mes_corrente not in {a.mes_ano for a in abertas}


def test_a_recusa_do_luna_no_modo_teste_fecha_o_ctn_real(tmp_path, monkeypatch):
    fechados = []
    monkeypatch.setattr(CTNEficaz, "fechar", lambda self: fechados.append(self))
    config = _config_de_teste(tmp_path, ctn_api_key=CHAVE_CTN)
    with pytest.raises(ValueError, match="AGENTE_LLM"):
        montar_dependencias(config)
    assert fechados == []  # o CTN só é montado depois das travas, como sempre


def test_montar_app_recusa_o_telefone_do_piloto_com_cobranca_real_ativa(tmp_path):
    banco = tmp_path / "teste.db"
    fabrica = criar_sessao(f"sqlite:///{banco}")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        sessao.add(
            Cobranca(
                cliente=Cliente(matricula_ctn="REAL-1", nome="Fictício", telefone=TESTADOR_1),
                regua=Regua.R3,
                mensalidades_abertas=3,
                divida_centavos=10020,
                propenso_cartao=False,
                campanha="ctn",
            )
        )
        sessao.commit()
    fabrica.kw["bind"].dispose()
    config = _config_de_teste(tmp_path, database_url=f"sqlite:///{banco}")
    with pytest.raises(ValueError, match="cobran"):
        montar_app(config, iniciar_trabalhador=False)


def test_montar_app_com_o_modo_teste_sobe_com_o_banco_sem_cobranca_real(tmp_path):
    banco = tmp_path / "teste.db"
    fabrica = criar_sessao(f"sqlite:///{banco}")
    Base.metadata.create_all(fabrica.kw["bind"])
    fabrica.kw["bind"].dispose()
    config = _config_de_teste(tmp_path, database_url=f"sqlite:///{banco}")
    with TestClient(montar_app(config, iniciar_trabalhador=False)):
        pass


def test_montar_app_sem_o_modo_teste_nao_consulta_o_banco():
    # O banco sem tabelas (em memória, vazio): a trava de subida só roda com o `[teste]`.
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)):
        pass
