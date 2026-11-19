from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agente.api.principal import montar_app, montar_dependencias
from agente.config import Config
from agente.db.modelos import Base, Cliente, Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootHttp
from agente.integracoes.ctn import ClienteNaoEncontrado, CTNComposto, CTNFalso
from agente.integracoes.ctn_eficaz import CTNEficaz
from agente.integracoes.ctn_teste import CTNDeCasos, CTNEscritaDeTeste, RoteadorCTN
from agente.integracoes.llm_turno import LLMTurnoOpenAI
from agente.integracoes.pix import PixDeTeste
from agente.integracoes.transcritor import TranscritorOpenAI
from agente.politica import Regua

from ..conftest import RAIZ, TESTADOR_1, escrever_politica_de_teste

CHAVE_LUNA = "chave-do-luna"
CHATWOOT_REAL = {
    "chatwoot_url": "https://chatwoot.invalid",
    "chatwoot_conta": 7,
    "chatwoot_token": "token-do-chatwoot",
}


def _config(**campos):
    # O serviço só sobe com o Chatwoot real e com a chave do Luna (D59, D61): o padrão dos testes os
    # traz, com a liberação da OpenAI (M2), que o Chatwoot real com o Luna sempre pede.
    padrao = {
        "database_url": "sqlite://",
        "politica_path": RAIZ / "politica.toml",
        "webhook_segredo": "s3gredo",
        "llm_api_key": CHAVE_LUNA,
        "llm_dados_reais_liberados": True,
        **CHATWOOT_REAL,
    }
    return Config(**{**padrao, **campos})


def _fecha(deps):
    if isinstance(deps.chatwoot, ChatwootHttp):
        deps.chatwoot.fechar()
    if hasattr(deps.ctn, "fechar"):
        deps.ctn.fechar()
    deps.llm_turno.fechar()


@pytest.mark.parametrize(
    "campos",
    [
        {"chatwoot_url": None, "chatwoot_conta": None, "chatwoot_token": None},
        {"chatwoot_token": None},  # pela metade também não vale
        {"chatwoot_url": None},
    ],
)
def test_sem_o_chatwoot_real_o_servico_nao_sobe(campos):
    with pytest.raises(ValueError, match="Chatwoot"):
        montar_dependencias(_config(**campos))


def test_com_chatwoot_configurado_usa_a_real_e_o_ctn_falso_aceita_a_matricula():
    deps = montar_dependencias(_config())
    try:
        assert isinstance(deps.chatwoot, ChatwootHttp)
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        # Até a T9 o CTN é o falso; ele aceita a matrícula da carteira.
        assert deps.ctn.consultar_cliente("DEMO-0001").matricula == "DEMO-0001"
    finally:
        _fecha(deps)


def test_sem_a_chave_do_llm_o_servico_nao_sobe():
    with pytest.raises(ValueError, match="AGENTE_LLM_API_KEY"):
        montar_dependencias(_config(llm_api_key=None))


@pytest.mark.parametrize(
    ("campos", "trecho"),
    [
        ({"llm_api_key": None}, "AGENTE_LLM_API_KEY"),
        ({"llm_dados_reais_liberados": False}, "AGENTE_LLM_DADOS_REAIS_LIBERADOS"),
    ],
)
def test_o_chatwoot_real_e_fechado_se_a_subida_for_recusada(monkeypatch, campos, trecho):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append(self))
    with pytest.raises(ValueError, match=trecho):
        montar_dependencias(_config(**campos))
    assert len(fechados) == 1


def test_com_a_chave_do_llm_o_turno_e_o_luna_real(caplog):
    caplog.set_level("INFO")
    deps = montar_dependencias(_config(llm_modelo="m-teste"))
    try:
        assert isinstance(deps.llm_turno, LLMTurnoOpenAI)
        assert deps.llm_turno.modelo == "m-teste"
        assert CHAVE_LUNA not in caplog.text
    finally:
        _fecha(deps)


def test_o_luna_sai_com_o_gpt_6_luna_e_o_reasoning_effort_por_padrao():
    deps = montar_dependencias(_config())
    try:
        assert deps.llm_turno.modelo == "gpt-6-luna"
        assert deps.llm_turno.corpo_extra == {"reasoning_effort": "none"}
    finally:
        _fecha(deps)


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
        _fecha(deps)


def test_sem_a_transcricao_configurada_o_audio_nao_e_transcrito(caplog):
    deps = montar_dependencias(_config())
    assert deps.transcritor is None
    assert "transcrição" in caplog.text.lower()
    _fecha(deps)


def test_com_a_transcricao_configurada_o_transcritor_e_o_real():
    config = _config(
        transcricao_url="https://transcricao.invalid/v1", transcricao_api_key="chave-ficticia"
    )
    deps = montar_dependencias(config)
    assert isinstance(deps.transcritor, TranscritorOpenAI)
    assert deps.transcritor.modelo == "gpt-4o-mini-transcribe"
    assert deps.transcritor.max_caracteres == deps.politica.transcricao_max_caracteres
    deps.transcritor.fechar()
    _fecha(deps)


def test_app_de_producao_responde_a_saude():
    with TestClient(montar_app(_config(), iniciar_trabalhador=False)) as cliente:
        assert cliente.get("/saude").json() == {"status": "ok"}


# ---- o CTN real (EFICAZ) e a trava do Luna (M2) ----

CHAVE_CTN = "eg_SEGREDO_INVENTADO_123"


def test_sem_a_chave_do_ctn_o_falso_de_hoje_serve():
    deps = montar_dependencias(_config())
    assert isinstance(deps.ctn, CTNFalso)
    _fecha(deps)


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


TRANSCRICAO = {
    "transcricao_url": "https://transcricao.invalid/v1",
    "transcricao_api_key": "chave-da-transcricao",
}
CTN_REAL = {"ctn_api_key": CHAVE_CTN}


# (fonte real a mais, destino externo a mais, o que a mensagem de recusa tem de nomear). O Chatwoot
# real e o Luna estão sempre ligados: o serviço não sobe sem eles (D59, D61).
BASE = ["AGENTE_CHATWOOT_*", "AGENTE_LLM_API_KEY"]
RECUSADAS = {
    "base": ({}, {}, BASE),
    "ctn": (CTN_REAL, {}, [*BASE, "AGENTE_CTN_API_KEY"]),
    "transcricao": ({}, TRANSCRICAO, [*BASE, "AGENTE_TRANSCRICAO_*"]),
    "ctn+transcricao": (
        CTN_REAL,
        TRANSCRICAO,
        [*BASE, "AGENTE_CTN_API_KEY", "AGENTE_TRANSCRICAO_*"],
    ),
}


@pytest.mark.parametrize("combinacao", RECUSADAS)
def test_fonte_real_com_destino_externo_sem_a_liberacao_o_servico_nao_sobe(combinacao):
    fonte, destino, nomes = RECUSADAS[combinacao]
    with pytest.raises(ValueError, match="AGENTE_LLM_DADOS_REAIS_LIBERADOS") as erro:
        montar_dependencias(_config(**fonte, **destino, llm_dados_reais_liberados=False))
    mensagem = str(erro.value)
    assert all(nome in mensagem for nome in nomes)
    # Só nomeia o que disparou.
    assert ("AGENTE_CTN_API_KEY" in nomes) == ("AGENTE_CTN_API_KEY" in mensagem)
    assert ("AGENTE_TRANSCRICAO_*" in nomes) == ("AGENTE_TRANSCRICAO_*" in mensagem)
    for segredo in (
        CHAVE_CTN,
        CHAVE_LUNA,
        "chave-da-transcricao",
        "token-do-chatwoot",
        "chatwoot.invalid",
        "transcricao.invalid",
    ):
        assert segredo not in mensagem


@pytest.mark.parametrize("combinacao", RECUSADAS)
def test_fonte_real_com_destino_externo_e_a_liberacao_o_servico_sobe(combinacao):
    fonte, destino, _ = RECUSADAS[combinacao]
    deps = montar_dependencias(_config(**fonte, **destino))
    _fecha(deps)


def test_ctn_real_com_o_luna_real_sem_a_liberacao_o_servico_nao_sobe():
    config = _config(ctn_api_key=CHAVE_CTN, llm_dados_reais_liberados=False)
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
        _fecha(deps)


def test_a_recusa_do_luna_fecha_o_chatwoot_e_o_ctn_real(monkeypatch):
    fechados = []
    monkeypatch.setattr(ChatwootHttp, "fechar", lambda self: fechados.append("chatwoot"))
    monkeypatch.setattr(CTNEficaz, "fechar", lambda self: fechados.append("ctn"))
    config = _config(ctn_api_key=CHAVE_CTN, llm_api_key=None)
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
    _fecha(deps)


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
    assert deps.ctn.consultar_cliente("SIM-3-7777").nome == "Carla Mendes"
    assert len(deps.ctn.mensalidades_em_aberto("SIM-3-7777")) == 4
    assert deps.ctn.gerar_link_pagamento("SIM-3-7777", 13360, 1).url == "https://ctnfalsoteste.com"


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
    config = _config_de_teste(tmp_path, ctn_api_key=CHAVE_CTN, llm_api_key=None)
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
