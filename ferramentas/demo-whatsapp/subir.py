"""Sobe a demo do agente no WhatsApp: o serviço em modo ativo, local, atrás de um túnel Cloudflare.

Uso, de dentro de repo/:
    python -m uv run python ferramentas/demo-whatsapp/subir.py --kit <pasta do kit>

O kit é uma pasta fora do git com o `politica-demo.toml` e a `carteira-demo.csv`; o script grava
nela `demo.db`, `segredo_webhook.txt`, `servico.log`, `tunel.log` e `url_tunel.txt`. As chaves vêm
de `<segredos>/chatwoot-teste.local.json`, `typesafe.local.json` e `openai.local.json`; elas só
entram no ambiente dos subprocessos, nunca num .env nem no log. A URL do túnel leva o segredo do
webhook, então ela só é impressa mascarada.

Passos: ambiente, banco e carteira, túnel, URLs no Chatwoot, serviço. O webhook que já existe no
Chatwoot só tem a URL trocada (PATCH): este script não cria nem apaga webhook.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLOUDFLARED = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
# O webhook da conta de teste que aponta para o agente; o script só troca a URL dele.
WEBHOOK_ID = 14
EVENTOS_DO_WEBHOOK = ["message_created", "conversation_status_changed"]
CAMINHO_DO_WEBHOOK = "/webhooks/chatwoot/"
MARCA_DO_SEGREDO = "<segredo>"
PRAZO_DE_ESPERA_SEGUNDOS = 30

ARQUIVO_CHATWOOT = "chatwoot-teste.local.json"
ARQUIVO_TYPESAFE = "typesafe.local.json"
ARQUIVO_OPENAI = "openai.local.json"

# `api.trycloudflare.com` aparece no log do cloudflared (é quem cria o túnel): não é o túnel.
_URL_DO_TUNEL = re.compile(r"https://(?!api\.)[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


class ErroDaDemo(Exception):
    """Falha que o operador resolve sem olhar o código: a mensagem diz o que fazer."""


def ler_json(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ErroDaDemo(f"falta o arquivo {caminho.name} em {caminho.parent}") from None
    except json.JSONDecodeError:
        raise ErroDaDemo(f"{caminho.name} não é um JSON válido") from None


def _exigir(segredos: Mapping, chaves: tuple[str, ...], arquivo: str) -> None:
    faltando = [chave for chave in chaves if not segredos.get(chave)]
    if faltando:
        raise ErroDaDemo(f"faltam as chaves {', '.join(faltando)} em {arquivo}")


def validar_segredos(chatwoot: Mapping, typesafe: Mapping, openai: Mapping) -> None:
    """Falha cedo, antes de subir qualquer coisa. Só os nomes das chaves entram nas mensagens."""
    _exigir(chatwoot, ("url", "conta", "inbox", "token"), ARQUIVO_CHATWOOT)
    if not chatwoot.get("token_bot"):
        raise ErroDaDemo(
            f"falta o token_bot em {ARQUIVO_CHATWOOT}: rode a D.4 antes. O agente precisa "
            "responder com o token do Agent Bot, não com o do usuário, ou o eco da resposta vira "
            '"humano assumiu"'
        )
    if chatwoot["token_bot"] == chatwoot["token"]:
        raise ErroDaDemo(
            f"o token_bot de {ARQUIVO_CHATWOOT} é igual ao token do usuário: o agente precisa do "
            "token do Agent Bot (D.4)"
        )
    _exigir(typesafe, ("api_key",), ARQUIVO_TYPESAFE)
    _exigir(openai, ("api_key",), ARQUIVO_OPENAI)


def montar_ambiente(
    *,
    chatwoot: Mapping,
    typesafe: Mapping,
    openai: Mapping,
    kit: Path,
    segredo_webhook: str,
    ambiente_base: Mapping[str, str],
    turno: str = "pipeline",
) -> dict[str, str]:
    """O ambiente dos subprocessos: o do processo, sem nenhuma AGENTE_* herdada, mais as da demo.

    O `turno` é o `AGENTE_TURNO` (D47): `pipeline`, o padrão, ou `uma_chamada`, o experimento.

    Os padrões de URL e de modelo do Jev e do Luna vêm do código (config.py): não se repetem aqui.
    """
    validar_segredos(chatwoot, typesafe, openai)
    ambiente = {c: v for c, v in ambiente_base.items() if not c.upper().startswith("AGENTE_")}
    ambiente.update(
        {
            "PYTHONUTF8": "1",
            "AGENTE_MODO": "ativo",
            "AGENTE_TURNO": turno,
            "AGENTE_POLITICA_PATH": str(kit / "politica-demo.toml"),
            "AGENTE_DATABASE_URL": f"sqlite:///{(kit / 'demo.db').resolve().as_posix()}",
            "AGENTE_WEBHOOK_SEGREDO": segredo_webhook,
            "AGENTE_CHATWOOT_URL": str(chatwoot["url"]),
            "AGENTE_CHATWOOT_CONTA": str(chatwoot["conta"]),
            "AGENTE_CHATWOOT_INBOX": str(chatwoot["inbox"]),
            # O token do Agent Bot, e não o do usuário: com o do usuário, a resposta do agente
            # voltaria no webhook como mensagem de um atendente e viraria "humano assumiu".
            "AGENTE_CHATWOOT_TOKEN": str(chatwoot["token_bot"]),
            "AGENTE_JEV_API_KEY": str(typesafe["api_key"]),
            # A demo manda ao Jev real só texto inventado (a carteira de teste e o celular do Cauê),
            # então a trava da ZDR (D12) é liberada só neste processo:
            # decisions/2026-11-04-demo-whatsapp-jev-real-dado-inventado.md
            "AGENTE_JEV_DADOS_REAIS_LIBERADOS": "true",
            "AGENTE_LLM_API_KEY": str(openai["api_key"]),
            # Mesma razão: o Chatwoot é o de teste e o dado é inventado; a trava da OpenAI (M2)
            # barra o Chatwoot real com o Luna real sem esta liberação.
            "AGENTE_LLM_DADOS_REAIS_LIBERADOS": "true",
            # O Config também lê o `.env` do cwd (repo/): vazio desliga o CTN real mesmo que o
            # `.env` traga a chave, para a demo nunca juntar o CTN real à liberação forçada acima.
            "AGENTE_CTN_API_KEY": "",
        }
    )
    return ambiente


def ler_ou_criar_segredo_do_webhook(
    arquivo: Path, gerar: Callable[[], str] = lambda: secrets.token_urlsafe(32)
) -> str:
    """Gera o segredo na 1ª vez e o relê depois: a URL do webhook não muda entre as subidas."""
    if arquivo.exists():
        segredo = arquivo.read_text(encoding="utf-8").strip()
        if segredo:
            return segredo
    segredo = gerar()
    arquivo.write_text(segredo, encoding="utf-8")
    return segredo


def achar_url_do_tunel(texto: str) -> str | None:
    """A URL `https://*.trycloudflare.com` do túnel no log do cloudflared, ou None."""
    achado = _URL_DO_TUNEL.search(texto)
    return achado.group(0) if achado else None


def url_do_webhook(url_do_tunel: str, segredo: str) -> str:
    return f"{url_do_tunel.rstrip('/')}{CAMINHO_DO_WEBHOOK}{segredo}"


def mascarar_segredo(texto: str, segredo: str) -> str:
    """Troca o segredo por <segredo>, onde estiver e no caminho do webhook, mesmo que seja outro."""
    if not segredo:
        raise ValueError("o segredo não pode ser vazio")
    texto = texto.replace(segredo, MARCA_DO_SEGREDO)
    return re.sub(rf"({re.escape(CAMINHO_DO_WEBHOOK)})[^/?#\s]+", rf"\1{MARCA_DO_SEGREDO}", texto)


def corpo_do_patch_do_webhook(url: str) -> dict:
    return {"webhook": {"url": url, "subscriptions": EVENTOS_DO_WEBHOOK}}


def corpo_do_patch_do_bot(url: str) -> dict:
    # O Chatwoot aceita o campo solto (o Rails o embrulha em `agent_bot` sozinho).
    return {"outgoing_url": url}


def _patch(url: str, token_do_usuario: str, corpo: dict) -> int:
    pedido = urllib.request.Request(
        url,
        data=json.dumps(corpo).encode("utf-8"),
        method="PATCH",
        headers={"api_access_token": token_do_usuario, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=15) as resposta:
            return resposta.status
    except urllib.error.HTTPError as erro:
        # Sem o corpo da resposta: o Chatwoot pode devolver a URL nova, que leva o segredo.
        raise ErroDaDemo(f"o Chatwoot recusou o PATCH (HTTP {erro.code})") from None
    except urllib.error.URLError as erro:
        raise ErroDaDemo(
            f"não consegui falar com o Chatwoot ({type(erro.reason).__name__})"
        ) from None


def atualizar_urls_no_chatwoot(chatwoot: Mapping, url_publica: str, segredo: str) -> None:
    base = f"{str(chatwoot['url']).rstrip('/')}/api/v1/accounts/{chatwoot['conta']}"
    token = str(chatwoot["token"])
    _patch(f"{base}/webhooks/{WEBHOOK_ID}", token, corpo_do_patch_do_webhook(url_publica))
    print(f"webhook {WEBHOOK_ID} atualizado: {mascarar_segredo(url_publica, segredo)}")
    if chatwoot.get("bot_id"):
        _patch(f"{base}/agent_bots/{chatwoot['bot_id']}", token, corpo_do_patch_do_bot(url_publica))
        print(
            f"Agent Bot {chatwoot['bot_id']} atualizado: {mascarar_segredo(url_publica, segredo)}"
        )
    else:
        print("sem bot_id em chatwoot-teste.local.json: o outgoing_url do Agent Bot não foi mexido")


def _executavel_do_cloudflared() -> str:
    if CLOUDFLARED.exists():
        return str(CLOUDFLARED)
    achado = shutil.which("cloudflared")
    if not achado:
        raise ErroDaDemo(f"não achei o cloudflared (esperado em {CLOUDFLARED})")
    return achado


def _esperar(condicao: Callable[[], object], descricao: str, processo: subprocess.Popen) -> object:
    limite = time.monotonic() + PRAZO_DE_ESPERA_SEGUNDOS
    while time.monotonic() < limite:
        resultado = condicao()
        if resultado:
            return resultado
        if processo.poll() is not None:
            raise ErroDaDemo(f"o processo terminou antes de {descricao}: veja o log no kit")
        time.sleep(0.5)
    raise ErroDaDemo(f"passaram {PRAZO_DE_ESPERA_SEGUNDOS} s sem {descricao}: veja o log no kit")


def _servico_responde(porta: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/saude", timeout=2) as resposta:
            return resposta.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _encerrar(processos: list[subprocess.Popen]) -> None:
    for processo in processos:
        if processo.poll() is None:
            processo.terminate()
    for processo in processos:
        try:
            processo.wait(timeout=10)
        except subprocess.TimeoutExpired:
            processo.kill()


def preparar_banco(kit: Path, ambiente: Mapping[str, str], carteira: Path) -> None:
    """Migra o `demo.db` do kit e, se ele ainda não existia, importa a carteira.

    Com o banco já criado, a carteira não é reimportada: as conversas do ensaio anterior ficam.
    """
    banco_existia = (kit / "demo.db").exists()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO, env=ambiente, check=True
    )
    if not banco_existia:
        subprocess.run(
            [sys.executable, "-m", "agente.cli", "importar", str(carteira), "--campanha", "demo"],
            cwd=REPO,
            env=ambiente,
            check=True,
        )


def subir(kit: Path, pasta_segredos: Path, porta: int, turno: str = "pipeline") -> int:
    for arquivo in ("politica-demo.toml", "carteira-demo.csv"):
        if not (kit / arquivo).is_file():
            raise ErroDaDemo(f"falta {arquivo} no kit ({kit})")
    chatwoot = ler_json(pasta_segredos / ARQUIVO_CHATWOOT)
    typesafe = ler_json(pasta_segredos / ARQUIVO_TYPESAFE)
    openai = ler_json(pasta_segredos / ARQUIVO_OPENAI)
    segredo = ler_ou_criar_segredo_do_webhook(kit / "segredo_webhook.txt")
    ambiente = montar_ambiente(
        chatwoot=chatwoot,
        typesafe=typesafe,
        openai=openai,
        kit=kit,
        segredo_webhook=segredo,
        ambiente_base=os.environ,
        turno=turno,
    )

    preparar_banco(kit, ambiente, kit / "carteira-demo.csv")

    processos: list[subprocess.Popen] = []
    try:
        with (kit / "tunel.log").open("wb") as log_do_tunel:
            tunel = subprocess.Popen(
                [
                    _executavel_do_cloudflared(),
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{porta}",
                    "--no-autoupdate",
                ],
                stdout=log_do_tunel,
                stderr=subprocess.STDOUT,
            )
        processos.append(tunel)
        url_do_tunel = str(
            _esperar(
                lambda: achar_url_do_tunel((kit / "tunel.log").read_text("utf-8", "replace")),
                "o túnel dar a URL",
                tunel,
            )
        )
        (kit / "url_tunel.txt").write_text(url_do_tunel, encoding="utf-8")

        atualizar_urls_no_chatwoot(chatwoot, url_do_webhook(url_do_tunel, segredo), segredo)

        with (kit / "servico.log").open("wb") as log_do_servico:
            servico = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "agente.api.principal:montar_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(porta),
                ],
                cwd=REPO,
                env=ambiente,
                stdout=log_do_servico,
                stderr=subprocess.STDOUT,
            )
        processos.append(servico)
        _esperar(lambda: _servico_responde(porta), "o serviço responder em /saude", servico)

        print(f"turno da demo: {turno}")
        print(f"demo no ar: {mascarar_segredo(url_do_webhook(url_do_tunel, segredo), segredo)}")
        print("Ctrl+C encerra o serviço e o túnel.")
        while all(processo.poll() is None for processo in processos):
            time.sleep(1)
        print("um dos processos terminou: veja servico.log e tunel.log no kit")
        return 1
    except KeyboardInterrupt:
        print("encerrando a demo")
        return 0
    finally:
        _encerrar(processos)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Sobe a demo do agente no WhatsApp.")
    parser.add_argument("--kit", type=Path, required=True, help="pasta do kit da demo")
    parser.add_argument(
        "--segredos",
        type=Path,
        default=REPO.parent / "secrets",
        help="pasta dos *.local.json (padrão: ../secrets, ao lado do repo)",
    )
    parser.add_argument("--porta", type=int, default=8000)
    parser.add_argument(
        "--turno",
        choices=("pipeline", "uma_chamada"),
        default="pipeline",
        help="o AGENTE_TURNO da demo (D47): o pipeline do Jev ou o turno de uma chamada",
    )
    argumentos = parser.parse_args(argv)
    try:
        return subir(
            argumentos.kit.resolve(),
            argumentos.segredos.resolve(),
            argumentos.porta,
            argumentos.turno,
        )
    except ErroDaDemo as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
