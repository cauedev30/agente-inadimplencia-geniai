"""O serviço HTTP: o webhook do Chatwoot, a saúde e o laço que roda os turnos."""

import hmac
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from agente.api.chatwoot_webhook import receber_evento
from agente.config import Config
from agente.politica import Politica
from agente.turno.fila import Processar, Trabalhador, processar_sem_acao


def _agora() -> datetime:
    return datetime.now(UTC)


def criar_app(
    fabrica: sessionmaker[Session],
    politica: Politica,
    config: Config,
    *,
    processar: Processar = processar_sem_acao,
    relogio: Callable[[], datetime] = _agora,
    iniciar_trabalhador: bool = True,
) -> FastAPI:
    trabalhador = Trabalhador(fabrica, processar, relogio)

    @asynccontextmanager
    async def ciclo_de_vida(_app: FastAPI):
        if iniciar_trabalhador:
            trabalhador.iniciar()
        try:
            yield
        finally:
            trabalhador.parar()

    app = FastAPI(title="Agente de inadimplência", lifespan=ciclo_de_vida)
    app.state.trabalhador = trabalhador

    @app.get("/saude")
    def saude() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/chatwoot/{segredo}")
    async def webhook_chatwoot(segredo: str, request: Request) -> dict:
        if config.webhook_segredo is None:
            raise HTTPException(503, "o webhook ainda não tem segredo configurado")
        if not hmac.compare_digest(segredo.encode(), config.webhook_segredo.encode()):
            raise HTTPException(401, "segredo inválido")
        try:
            dados = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "o corpo não é um JSON válido") from None
        if not isinstance(dados, dict):
            raise HTTPException(400, "o corpo tem de ser um objeto JSON")

        def _tratar() -> str:
            with fabrica() as sessao:
                return receber_evento(sessao, dados, politica, config, relogio())

        return {"resultado": await run_in_threadpool(_tratar)}

    return app
