"""O script da demo no WhatsApp: só as funções puras, sem rede, subprocesso nem segredo real."""

import importlib.util
import os

import pytest

from ..conftest import RAIZ

_CAMINHO = RAIZ / "ferramentas" / "demo-whatsapp" / "subir.py"
_spec = importlib.util.spec_from_file_location("demo_whatsapp_subir", _CAMINHO)
subir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(subir)

CHATWOOT = {
    "url": "https://chatwoot.invalid",
    "conta": 91,
    "inbox": 280,
    "token": "token-do-usuario-ficticio",
    "token_bot": "token-do-bot-ficticio",
    "bot_id": 5,
}
OPENAI = {"api_key": "chave-openai-ficticia"}
SEGREDO = "segredo-ficticio-do-webhook"


def _ambiente(tmp_path, **trocas):
    campos = {
        "chatwoot": CHATWOOT,
        "openai": OPENAI,
        "kit": tmp_path,
        "segredo_webhook": SEGREDO,
        "ambiente_base": {},
    }
    return subir.montar_ambiente(**{**campos, **trocas})


def test_o_ambiente_leva_a_liberacao_do_luna_so_para_a_demo(tmp_path):
    ambiente = _ambiente(tmp_path)
    assert ambiente["AGENTE_LLM_DADOS_REAIS_LIBERADOS"] == "true"
    assert ambiente["AGENTE_LLM_API_KEY"] == OPENAI["api_key"]
    assert ambiente["AGENTE_WEBHOOK_SEGREDO"] == SEGREDO
    assert ambiente["AGENTE_CHATWOOT_CONTA"] == "91"
    assert ambiente["AGENTE_CHATWOOT_INBOX"] == "280"
    assert ambiente["AGENTE_POLITICA_PATH"] == str(tmp_path / "politica-demo.toml")


def test_o_agente_responde_com_o_token_do_bot_e_nao_com_o_do_usuario(tmp_path):
    ambiente = _ambiente(tmp_path)
    assert ambiente["AGENTE_CHATWOOT_TOKEN"] == CHATWOOT["token_bot"]
    assert CHATWOOT["token"] not in ambiente.values()


def test_o_banco_da_demo_usa_barras_normais(tmp_path):
    url = _ambiente(tmp_path)["AGENTE_DATABASE_URL"]
    assert url.startswith("sqlite:///")
    assert "\\" not in url
    assert url.endswith("/demo.db")


def test_o_ambiente_tira_as_agente_herdadas_e_guarda_o_resto(tmp_path):
    herdado = {"PATH": "/bin", "AGENTE_CTN_WEBHOOK_SEGREDO": "x", "agente_log_level": "DEBUG"}
    ambiente = _ambiente(tmp_path, ambiente_base=herdado)
    assert ambiente["PATH"] == "/bin"
    assert "AGENTE_CTN_WEBHOOK_SEGREDO" not in ambiente
    assert "agente_log_level" not in ambiente


def test_a_herdada_nao_sobrepoe_a_da_demo(tmp_path):
    ambiente = _ambiente(tmp_path, ambiente_base={"AGENTE_WEBHOOK_SEGREDO": "herdado"})
    assert ambiente["AGENTE_WEBHOOK_SEGREDO"] == SEGREDO


def test_sem_o_token_do_bot_falha_cedo_e_manda_rodar_a_d4(tmp_path):
    sem_bot = {k: v for k, v in CHATWOOT.items() if k != "token_bot"}
    with pytest.raises(subir.ErroDaDemo, match="D.4") as erro:
        _ambiente(tmp_path, chatwoot=sem_bot)
    assert "token_bot" in str(erro.value)
    assert CHATWOOT["token"] not in str(erro.value)


def test_o_token_do_bot_igual_ao_do_usuario_e_recusado(tmp_path):
    igual = {**CHATWOOT, "token_bot": CHATWOOT["token"]}
    with pytest.raises(subir.ErroDaDemo, match="igual"):
        _ambiente(tmp_path, chatwoot=igual)


def test_falta_a_chave_da_api(tmp_path):
    with pytest.raises(subir.ErroDaDemo, match="api_key"):
        _ambiente(tmp_path, openai={})


def test_o_segredo_do_webhook_e_criado_uma_vez_e_relido(tmp_path):
    arquivo = tmp_path / "segredo_webhook.txt"
    primeiro = subir.ler_ou_criar_segredo_do_webhook(arquivo, gerar=lambda: "um")
    segundo = subir.ler_ou_criar_segredo_do_webhook(arquivo, gerar=lambda: "dois")
    assert (primeiro, segundo) == ("um", "um")


def test_o_segredo_gerado_por_padrao_e_longo_e_unico(tmp_path):
    a = subir.ler_ou_criar_segredo_do_webhook(tmp_path / "a.txt")
    b = subir.ler_ou_criar_segredo_do_webhook(tmp_path / "b.txt")
    assert len(a) >= 32 and a != b


def test_a_url_do_tunel_e_achada_no_log_do_cloudflared():
    log = (
        "INF Requesting new quick Tunnel on trycloudflare.com...\n"
        'ERR Post "https://api.trycloudflare.com/tunnel": falha\n'
        "INF |  https://gato-azul-casa-9.trycloudflare.com  |\n"
        "INF Registered tunnel connection\n"
    )
    assert subir.achar_url_do_tunel(log) == "https://gato-azul-casa-9.trycloudflare.com"


def test_sem_url_no_log_devolve_none():
    assert (
        subir.achar_url_do_tunel("INF Starting tunnel\nERR https://api.trycloudflare.com/x") is None
    )


def test_o_mascaramento_nunca_devolve_o_segredo():
    url = subir.url_do_webhook("https://gato.trycloudflare.com/", SEGREDO)
    assert url == f"https://gato.trycloudflare.com/webhooks/chatwoot/{SEGREDO}"
    mascarada = subir.mascarar_segredo(url, SEGREDO)
    assert SEGREDO not in mascarada
    assert mascarada == "https://gato.trycloudflare.com/webhooks/chatwoot/<segredo>"


def test_o_mascaramento_cobre_o_segredo_solto_e_o_de_outro_caminho():
    assert SEGREDO not in subir.mascarar_segredo(f"erro com {SEGREDO} no meio", SEGREDO)
    assert "outro" not in subir.mascarar_segredo("http://x/webhooks/chatwoot/outro?a=1", SEGREDO)


def test_o_mascaramento_recusa_segredo_vazio():
    with pytest.raises(ValueError):
        subir.mascarar_segredo("http://x", "")


def test_os_corpos_dos_patch():
    url = "https://gato.trycloudflare.com/webhooks/chatwoot/s"
    assert subir.corpo_do_patch_do_webhook(url) == {
        "webhook": {
            "url": url,
            "subscriptions": ["message_created", "conversation_status_changed"],
        }
    }
    assert subir.corpo_do_patch_do_bot(url) == {"outgoing_url": url}


def test_o_ambiente_desliga_o_ctn_real_mesmo_com_a_chave_no_env_do_cwd(tmp_path, monkeypatch):
    from agente.config import Config

    (tmp_path / ".env").write_text("AGENTE_CTN_API_KEY=chave-ficticia-do-ctn\n", encoding="utf-8")
    ambiente = _ambiente(tmp_path, ambiente_base={"AGENTE_CTN_API_KEY": "chave-herdada"})
    assert ambiente["AGENTE_CTN_API_KEY"] == ""
    monkeypatch.chdir(tmp_path)
    for variavel in [v for v in os.environ if v.startswith("AGENTE_")]:
        monkeypatch.delenv(variavel)
    for variavel, valor in ambiente.items():
        if variavel.startswith("AGENTE_"):
            monkeypatch.setenv(variavel, valor)
    assert Config().ctn_real_ligado is False
