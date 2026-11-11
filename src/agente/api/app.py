"""O serviço HTTP: os webhooks do Chatwoot e dos pagamentos, a saúde e o laço que roda os turnos."""

import hmac
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from agente.api.chatwoot_webhook import receber_evento
from agente.api.ctn_webhook import criar_rota_ctn
from agente.api.pix_webhook import criar_rota_pix
from agente.api.webchat import criar_rota_webchat
from agente.config import Config
from agente.integracoes.ctn import CTN
from agente.pagamento.baixas_ctn import DependenciasDasBaixas, ler_baixas
from agente.pagamento.concluir import (
    DependenciasPagamento,
    concluir_pagamentos,
    contar_sem_concluir,
)
from agente.pagamento.expirar import expirar_pix
from agente.politica import Politica
from agente.tipos import Modo
from agente.turno.fila import Processar, Rotina, Trabalhador, a_cada, processar_sem_acao
from agente.webchat.amarrar import (
    contar_amarracoes_sem_concluir,
    da_inbox_do_webchat,
    refazer_amarracoes,
    tratar_evento_webchat,
)

log = logging.getLogger(__name__)

# O Pix vencido não tem pressa (G2.4): uma varredura por minuto basta.
INTERVALO_EXPIRAR_PIX = timedelta(minutes=1)
# A amarração do webchat pendente já espera por `espera_integracao`; a varredura é só a checagem.
INTERVALO_AMARRACOES = timedelta(seconds=10)
# O CTN coleta as baixas a cada 10 minutos (spec do CTN real, seção 2): ler mais só gasta cota.
INTERVALO_BAIXAS = timedelta(minutes=10)


def _agora() -> datetime:
    return datetime.now(UTC)


def _fechar_ctn(ctn: object) -> None:
    """Fecha o cliente HTTP do CTN real no encerramento; o falso não tem o que fechar."""
    fechar = getattr(ctn, "fechar", None)
    if callable(fechar):
        try:
            fechar()
        except Exception:
            log.exception("não foi possível fechar o cliente do CTN")


def _rotinas(
    dependencias: DependenciasPagamento | None,
    politica: Politica,
    config: Config,
    ctn: CTN | None = None,
) -> list[Rotina]:
    rotinas: list[Rotina] = [a_cada(INTERVALO_EXPIRAR_PIX, expirar_pix)]
    if dependencias is not None:
        # Os webhooks só gravam a confirmação; a baixa e a mensagem saem por esta rotina.
        def concluir(sessao: Session, agora: datetime) -> int:
            return concluir_pagamentos(sessao, dependencias, agora)

        rotinas.append(concluir)
        if config.ctn_real_ligado and ctn is not None:
            # As baixas que o cliente fez por fora do agente (boleto, cartão, balcão). Só o CTN
            # real tem o `lancamentos_bx`; o falso não tem o que ler.
            deps_baixas = DependenciasDasBaixas(
                politica=politica,
                chatwoot=dependencias.chatwoot,
                time_humano=dependencias.time_humano,
                # Sem o modo (dependências de teste), o seguro é a sombra: nada é enviado.
                modo=getattr(dependencias, "modo", Modo.SOMBRA),
                ctn=ctn,
            )

            def baixas(sessao: Session, agora: datetime) -> object:
                return ler_baixas(sessao, deps_baixas, agora)

            rotinas.append(a_cada(INTERVALO_BAIXAS, baixas))
        if config.webchat_inbox is not None:
            # A amarração do webchat que o Chatwoot não deixou terminar (tarefa 7.3).
            def refazer(sessao: Session, agora: datetime) -> int:
                return refazer_amarracoes(
                    sessao, dependencias.chatwoot, config, politica, dependencias.time_humano, agora
                )

            rotinas.append(a_cada(INTERVALO_AMARRACOES, refazer))
    return rotinas


def criar_app(
    fabrica: sessionmaker[Session],
    politica: Politica,
    config: Config,
    *,
    processar: Processar = processar_sem_acao,
    relogio: Callable[[], datetime] = _agora,
    iniciar_trabalhador: bool = True,
    dependencias: DependenciasPagamento | None = None,
    ctn: CTN | None = None,
) -> FastAPI:
    """`dependencias` são as do turno; os webhooks de pagamento as passam ao confirmar_pagamento.

    `ctn` confere o CPF da página do webchat; sem ele (ou sem os segredos), a página fica desligada.
    """
    trabalhador = Trabalhador(
        fabrica, processar, relogio, rotinas=_rotinas(dependencias, politica, config, ctn)
    )

    @asynccontextmanager
    async def ciclo_de_vida(_app: FastAPI):
        if iniciar_trabalhador:
            trabalhador.iniciar()
        try:
            yield
        finally:
            trabalhador.parar()
            _fechar_ctn(ctn)

    app = FastAPI(title="Agente de inadimplência", lifespan=ciclo_de_vida)
    app.state.trabalhador = trabalhador

    @app.get("/saude")
    def saude() -> dict:
        # Só diz que o processo está vivo, sem tocar no banco: é o que o health check consulta.
        return {"status": "ok"}

    @app.get("/saude/pagamentos")
    def saude_pagamentos() -> dict:
        # Os pagamentos pagos que a conclusão não consegue terminar: precisam de gente.
        try:
            with fabrica() as sessao:
                sem_concluir = contar_sem_concluir(sessao)
        except Exception:
            log.exception("a contagem dos pagamentos sem concluir falhou")
            raise HTTPException(503, "não foi possível contar os pagamentos sem concluir") from None
        return {"pagamentos_sem_concluir": sem_concluir}

    @app.get("/saude/webchat")
    def saude_webchat() -> dict:
        # As amarrações do webchat que esgotaram as tentativas: o atendente está sem o resumo.
        try:
            with fabrica() as sessao:
                sem_concluir = contar_amarracoes_sem_concluir(sessao)
        except Exception:
            log.exception("a contagem das amarrações do webchat falhou")
            raise HTTPException(503, "não foi possível contar as amarrações") from None
        return {"amarracoes_sem_concluir": sem_concluir}

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
                eventos_do_webchat = ("conversation_created", "message_created")
                if dados.get("event") in eventos_do_webchat and da_inbox_do_webchat(dados, config):
                    # A conversa do webchat: a amarração à cobrança (tarefa 7.3).
                    if dependencias is None:
                        return "ignorado"
                    return tratar_evento_webchat(
                        sessao,
                        dependencias.chatwoot,
                        dados,
                        config,
                        politica,
                        dependencias.time_humano,
                        relogio(),
                    )
                return receber_evento(
                    sessao,
                    dados,
                    politica,
                    config,
                    relogio(),
                    casos=getattr(dependencias, "casos", None),
                )

        return {"resultado": await run_in_threadpool(_tratar)}

    app.include_router(criar_rota_pix(fabrica, config.pix_webhook_segredo, dependencias))
    app.include_router(criar_rota_ctn(fabrica, config.ctn_webhook_segredo, dependencias))
    app.include_router(
        criar_rota_webchat(
            fabrica, politica, config, ctn, relogio, getattr(dependencias, "chatwoot", None)
        )
    )
    return app
