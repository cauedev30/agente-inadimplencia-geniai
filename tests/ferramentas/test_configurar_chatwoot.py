"""O script que aponta o Chatwoot da conta 91 para o agente no dev2 (2.4): as partes sem rede."""

import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "configurar_chatwoot", RAIZ / "ferramentas" / "teste" / "configurar_chatwoot.py"
)
configurar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(configurar)


def test_a_url_do_tunel_e_a_ultima_do_log():
    log = (
        "INF | https://api.trycloudflare.com/tunnel\n"
        "INF | https://velho-um-dois.trycloudflare.com\n"
        "INF | https://novo-tres-quatro.trycloudflare.com\n"
    )
    assert configurar.ultima_url_do_tunel(log) == "https://novo-tres-quatro.trycloudflare.com"


def test_sem_url_no_log_nao_inventa():
    assert configurar.ultima_url_do_tunel("INF | https://api.trycloudflare.com/tunnel") is None


def test_o_caminho_da_url_nao_sai_na_tela():
    url = "https://x-y.trycloudflare.com/webhooks/chatwoot/SEGREDO-LONGO"
    mostrado = configurar._so_o_host(url)
    assert "SEGREDO" not in mostrado
    assert mostrado.startswith("https://x-y.trycloudflare.com")


def test_recusa_o_bot_do_tiago_antes_de_falar_com_qualquer_servidor(tmp_path):
    (tmp_path / "chatwoot-teste.local.json").write_text(
        json.dumps(
            {"url": "https://c.exemplo", "conta": 1, "inbox": 2, "token": "a", "bot_id": 67}
        ),
        encoding="utf-8",
    )
    with pytest.raises(configurar.subir.ErroDaDemo, match="Tiago"):
        configurar.main(["--segredos", str(tmp_path), "--host", "host-que-nao-existe"])
