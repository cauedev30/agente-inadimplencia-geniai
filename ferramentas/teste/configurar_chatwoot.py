"""Aponta o Agent Bot e o webhook da conta de teste do Chatwoot para o agente do dev2 (2.4).

Lê do dev2, por SSH, a URL atual do túnel (muda a cada reinício do contêiner `tunel`) e o
segredo do webhook (do `agente.env`). Do Chatwoot lê o que já está lá. Os tokens vêm de
`secrets/`. Nada secreto é impresso: as URLs saem sem o segredo.

Sem `--aplicar` só mostra o que faria (nada é escrito). Com `--aplicar` troca a URL do webhook
14 e o `outgoing_url` do bot de `chatwoot-teste.local.json` (o 69), como o `subir.py` fazia na demo.
Não mexe no bot 67 (do Tiago) nem no `webhook_url` da inbox 280 (do gateway da GeniAI).

    python ferramentas/teste/configurar_chatwoot.py [--aplicar] [--host dev2]
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "demo_whatsapp_subir", RAIZ / "ferramentas" / "demo-whatsapp" / "subir.py"
)
subir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(subir)

PASTA_NO_SERVIDOR = "/opt/agente-inadimplencia"
BOT_DO_TIAGO = 67  # nunca mexer


def ultima_url_do_tunel(texto: str) -> str | None:
    """A última URL `https://*.trycloudflare.com` do log (o log guarda as de antes do reinício)."""
    achados = re.findall(subir._URL_DO_TUNEL.pattern, texto)
    return achados[-1] if achados else None


def _ssh(host: str, comando: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", host, comando],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def ler_do_servidor(host: str) -> tuple[str, str]:
    """A URL atual do túnel e o segredo do webhook, lidos do dev2."""
    log = _ssh(
        host,
        f"cd {PASTA_NO_SERVIDOR}/app && docker compose -f deploy/dev2/compose.yaml logs tunel 2>&1",
    )
    url = ultima_url_do_tunel(log)
    if url is None:
        raise subir.ErroDaDemo("não achei a URL do túnel no log do contêiner `tunel`")
    saida = _ssh(host, f"grep '^AGENTE_WEBHOOK_SEGREDO=' {PASTA_NO_SERVIDOR}/agente.env")
    segredo = saida.strip().partition("=")[2]
    if not segredo:
        raise subir.ErroDaDemo("o AGENTE_WEBHOOK_SEGREDO do agente.env está vazio")
    return url, segredo


def _get(chatwoot: dict, caminho: str) -> dict:
    base = f"{str(chatwoot['url']).rstrip('/')}/api/v1/accounts/{chatwoot['conta']}"
    pedido = urllib.request.Request(
        base + caminho, headers={"api_access_token": str(chatwoot["token"])}, method="GET"
    )
    with urllib.request.urlopen(pedido, timeout=20) as resposta:
        return json.loads(resposta.read())


def _bots(chatwoot: dict) -> list[dict]:
    achado = _get(chatwoot, "/agent_bots")
    return achado.get("payload", achado) if isinstance(achado, dict) else achado


def _webhooks(chatwoot: dict) -> list[dict]:
    achado = _get(chatwoot, "/webhooks")["payload"]
    return achado["webhooks"] if isinstance(achado, dict) else achado


def _so_o_host(url: str | None) -> str:
    return re.sub(r"(https?://[^/]+)/.*", r"\1/…", url) if url else "<vazio>"


def situacao(chatwoot: dict, bot_id: int, url_nova: str) -> tuple[str, str]:
    """O host que o webhook e o bot apontam hoje (sem o caminho, que leva o segredo)."""
    webhook = next((w for w in _webhooks(chatwoot) if w["id"] == subir.WEBHOOK_ID), {})
    bot = next((b for b in _bots(chatwoot) if b["id"] == bot_id), {})
    return _so_o_host(webhook.get("url")), _so_o_host(bot.get("outgoing_url"))


def main(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    analisador.add_argument("--aplicar", action="store_true", help="escreve no Chatwoot")
    analisador.add_argument("--host", default="dev2")
    analisador.add_argument("--segredos", type=Path, default=RAIZ.parent / "secrets")
    argumentos = analisador.parse_args(argv)

    chatwoot = subir.ler_json(argumentos.segredos / subir.ARQUIVO_CHATWOOT)
    bot_id = int(chatwoot["bot_id"])
    if bot_id == BOT_DO_TIAGO:
        raise subir.ErroDaDemo("o bot 67 é do Tiago: não mexer")

    url_tunel, segredo = ler_do_servidor(argumentos.host)
    url_nova = subir.url_do_webhook(url_tunel, segredo)
    hoje_webhook, hoje_bot = situacao(chatwoot, bot_id, url_nova)
    print(f"conta {chatwoot['conta']}: webhook {subir.WEBHOOK_ID} e bot {bot_id}")
    print(f"  hoje:   webhook {hoje_webhook} | bot {hoje_bot}")
    print(f"  depois: {subir.mascarar_segredo(url_nova, segredo)}")
    if not argumentos.aplicar:
        print("(nada foi escrito; use --aplicar)")
        return 0

    subir.atualizar_urls_no_chatwoot(chatwoot, url_nova, segredo)
    depois_webhook, depois_bot = situacao(chatwoot, bot_id, url_nova)
    esperado = _so_o_host(url_nova)
    print(f"  conferido: webhook {depois_webhook} | bot {depois_bot}")
    return 0 if depois_webhook == depois_bot == esperado else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subir.ErroDaDemo as erro:
        print(f"erro: {erro}", file=sys.stderr)
        sys.exit(2)
