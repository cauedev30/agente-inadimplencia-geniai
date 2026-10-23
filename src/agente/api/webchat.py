"""GET e POST /atendimento/{token}: a página do webchat, com o widget do Chatwoot (tarefa 7.2).

Segurança do link (ideia-webchat, seção 4): o token é validado no servidor; a página pede os
primeiros dígitos do CPF antes de qualquer coisa; só então carrega o widget, já identificado com o
`identifier_hash` calculado aqui (a chave nunca vai ao navegador). Nada da dívida, do nome, do
telefone ou do CPF vai ao HTML, ao título ou à URL, e toda resposta sai com `no-store`.
Token inválido, vencido ou revogado dá sempre o mesmo 404.
"""

import hmac
import html
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, literal, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from agente.config import Config
from agente.db.modelos import Cobranca
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.politica import Politica
from agente.webchat.bloqueio import avisar_bloqueio
from agente.webchat.identidade import hash_de_identidade, identificador_do_chatwoot
from agente.webchat.token import TokenInvalido, validar_token

log = logging.getLogger(__name__)

_CABECALHOS = {
    "Cache-Control": "no-store",
    # O token está na URL: ela não pode vazar por Referer nem entrar em buscador.
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}

_PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Atendimento</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:2rem 1rem;background:#f5f6f8;color:#1c2330}}
main{{max-width:26rem;margin:0 auto;background:#fff;border-radius:.5rem;padding:1.5rem}}
h1{{font-size:1.25rem;margin-top:0}}
input{{font-size:1.25rem;width:100%;box-sizing:border-box;padding:.5rem;margin:.5rem 0 1rem}}
button{{font-size:1rem;padding:.6rem 1.2rem}}
.erro{{color:#a12622}}
</style>
</head>
<body>
<main>
{corpo}
</main>
{script}
</body>
</html>
"""

_FORMULARIO = """<h1>Atendimento</h1>
<p>Para a sua segurança, digite os {digitos} primeiros dígitos do seu CPF.</p>
{aviso}
<form method="post" action="">
<input name="cpf" inputmode="numeric" autocomplete="off" maxlength="{digitos}" required autofocus>
<button type="submit">Continuar</button>
</form>"""

_SCRIPT_WIDGET = """<script>
window.chatwootSettings = {{locale: "pt_BR"}};
(function () {{
  var baseUrl = {base_url};
  var identificador = {identificador};
  var hash = {hash};
  window.addEventListener("chatwoot:ready", function () {{
    window.$chatwoot.setUser(identificador, {{identifier_hash: hash}});
    window.$chatwoot.toggle("open");
  }});
  var sdk = document.createElement("script");
  sdk.src = baseUrl + "/packs/js/sdk.js";
  sdk.async = true;
  sdk.onload = function () {{
    window.chatwootSDK.run({{websiteToken: {website_token}, baseUrl: baseUrl}});
  }};
  document.head.appendChild(sdk);
}})();
</script>"""


def _js(valor: str) -> str:
    """O texto como literal de JavaScript, sem poder fechar o <script>."""
    return json.dumps(valor).replace("<", "\\u003c").replace(">", "\\u003e")


def _resposta(corpo: str, status: int = 200, script: str = "") -> HTMLResponse:
    pagina = _PAGINA.format(corpo=corpo, script=script)
    return HTMLResponse(pagina, status_code=status, headers=_CABECALHOS)


def _nao_encontrado() -> HTMLResponse:
    return _resposta("<h1>Link inválido</h1><p>Este link não está mais disponível.</p>", 404)


def _bloqueado() -> HTMLResponse:
    corpo = (
        "<h1>Acesso bloqueado</h1>"
        "<p>Houve tentativas demais. Para continuar, fale com a gente pelo WhatsApp.</p>"
    )
    return _resposta(corpo, 403)


def _indisponivel() -> HTMLResponse:
    corpo = "<h1>Atendimento indisponível</h1><p>Tente de novo em alguns minutos.</p>"
    return _resposta(corpo, 503)


def _sem_como_confirmar() -> HTMLResponse:
    corpo = (
        "<h1>Não foi possível continuar</h1>"
        "<p>Não conseguimos confirmar os seus dados por aqui. Fale com a gente pelo WhatsApp.</p>"
    )
    return _resposta(corpo, 503)


def _formulario(politica: Politica, aviso: str = "") -> HTMLResponse:
    avisos = f'<p class="erro">{html.escape(aviso)}</p>' if aviso else ""
    return _resposta(_FORMULARIO.format(digitos=politica.webchat_cpf_digitos, aviso=avisos))


def criar_rota_webchat(
    fabrica: sessionmaker[Session],
    politica: Politica,
    config: Config,
    ctn: CTN | None,
    relogio: Callable[[], datetime],
    chatwoot: Chatwoot | None = None,
) -> APIRouter:
    roteador = APIRouter()
    # Sem qualquer um destes, a rota fica desligada: um HMAC de chave vazia qualquer um calcula.
    ligada = config.webchat_ligado and ctn is not None

    def _entrada(token: str, agora: datetime) -> tuple[int | None, HTMLResponse | None]:
        """O id da cobrança do token, ou a resposta de recusa."""
        with fabrica() as sessao:
            try:
                cobranca_id = validar_token(sessao, config.webchat_segredo or "", token, agora)
            except TokenInvalido:
                return None, _nao_encontrado()
            cobranca = sessao.get(Cobranca, cobranca_id)
            ate = cobranca.webchat_bloqueado_ate
            if ate is not None and ate <= agora:
                # O bloqueio acabou: as tentativas voltam a zero.
                sessao.execute(
                    update(Cobranca)
                    .where(Cobranca.id == cobranca_id, Cobranca.webchat_bloqueado_ate <= agora)
                    .values(webchat_tentativas_cpf=0, webchat_bloqueado_ate=None)
                )
                sessao.commit()
                return cobranca_id, None
            bloqueada = ate is not None or (
                cobranca.webchat_tentativas_cpf >= politica.webchat_cpf_tentativas
            )
        return (None, _bloqueado()) if bloqueada else (cobranca_id, None)

    def _avisar(sessao: Session, cobranca_id: int, tentativas: int) -> None:
        # Uma vez por bloqueio: só quem gasta a última tentativa chega aqui. Se o Chatwoot falha, o
        # cliente não é prejudicado; o bloqueio vale do mesmo jeito.
        if chatwoot is None:
            return
        try:
            avisar_bloqueio(
                sessao, chatwoot, cobranca_id, politica.webchat_cpf_bloqueio_horas, tentativas
            )
        except Exception:
            log.exception("webchat: o aviso do bloqueio da cobrança %s falhou", cobranca_id)

    def _conferir(cobranca_id: int, digitos: str, agora: datetime) -> HTMLResponse:
        assert ctn is not None
        with fabrica() as sessao:
            cobranca = sessao.get(Cobranca, cobranca_id)
            try:
                cpf = ctn.consultar_cliente(cobranca.cliente.matricula_ctn).cpf
            except (CTNIndisponivel, ClienteNaoEncontrado):
                # Falha nossa, não do cliente: não gasta tentativa.
                return _indisponivel()
            if not cpf:
                # Sem CPF no CTN a página nunca confirma: não adianta gastar as tentativas do
                # cliente, o atendente é quem resolve.
                log.warning("webchat: a cobrança %s não tem CPF no CTN", cobranca_id)
                return _sem_como_confirmar()
            # A tentativa é gasta antes de comparar e o UPDATE confere o limite, para as
            # requisições em paralelo não passarem do teto. O mesmo UPDATE que gasta a última
            # tentativa grava o fim do bloqueio.
            teto = politica.webchat_cpf_tentativas
            fim = literal(
                agora + timedelta(hours=politica.webchat_cpf_bloqueio_horas),
                Cobranca.webchat_bloqueado_ate.type,
            )
            gasta = sessao.execute(
                update(Cobranca)
                .where(
                    Cobranca.id == cobranca_id,
                    Cobranca.webchat_tentativas_cpf < teto,
                    Cobranca.webchat_bloqueado_ate.is_(None),
                )
                .values(
                    webchat_tentativas_cpf=Cobranca.webchat_tentativas_cpf + 1,
                    webchat_bloqueado_ate=case(
                        (Cobranca.webchat_tentativas_cpf + 1 >= teto, fim),
                        else_=Cobranca.webchat_bloqueado_ate,
                    ),
                )
                .returning(Cobranca.webchat_tentativas_cpf)
            )
            tentativas = gasta.scalar()
            sessao.commit()
            if tentativas is None:
                return _bloqueado()
            formato = re.fullmatch(f"[0-9]{{{politica.webchat_cpf_digitos}}}", digitos)
            confere = bool(
                formato
                and cpf
                and hmac.compare_digest(digitos, re.sub(r"\D", "", cpf)[: len(digitos)])
            )
            if not confere:
                log.info("webchat: dígitos do CPF não conferem (cobrança %s)", cobranca_id)
                if tentativas >= teto:
                    _avisar(sessao, cobranca_id, tentativas)
                    return _bloqueado()
                return _formulario(politica, "Os dígitos não conferem. Tente de novo.")
            sessao.execute(
                update(Cobranca)
                .where(Cobranca.id == cobranca_id)
                .values(
                    webchat_tentativas_cpf=0,
                    webchat_bloqueado_ate=None,
                    webchat_verificado_em=agora,
                )
            )
            sessao.commit()
        identificador = identificador_do_chatwoot(cobranca_id)
        script = _SCRIPT_WIDGET.format(
            base_url=_js(config.chatwoot_url.rstrip("/")),
            identificador=_js(identificador),
            hash=_js(hash_de_identidade(config.webchat_identidade_hmac, identificador)),
            website_token=_js(config.webchat_website_token),
        )
        return _resposta(
            "<h1>Atendimento</h1><p>Conectando você a um atendente…</p>", script=script
        )

    @roteador.get("/atendimento/{token}")
    async def pedir_cpf(token: str) -> HTMLResponse:
        if not ligada:
            return _indisponivel()
        _, recusa = await run_in_threadpool(_entrada, token, relogio())
        return recusa or _formulario(politica)

    @roteador.post("/atendimento/{token}")
    async def conferir_cpf(token: str, request: Request) -> HTMLResponse:
        if not ligada:
            return _indisponivel()
        # Sem a dependência de formulário (python-multipart): o corpo é só `cpf=...`.
        corpo = parse_qs((await request.body()).decode("utf-8", "replace"))
        digitos = (corpo.get("cpf") or [""])[0]
        cobranca_id, recusa = await run_in_threadpool(_entrada, token, relogio())
        if recusa is not None:
            return recusa
        return await run_in_threadpool(_conferir, cobranca_id, digitos, relogio())

    return roteador
