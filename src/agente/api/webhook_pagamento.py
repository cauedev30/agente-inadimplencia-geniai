"""O que os webhooks de pagamento (Pix e CTN) têm em comum: a assinatura, o payload e a resposta.

Payload fictício, até a T1 (banco do Pix) e a T9 (CTN) dizerem o formato real:

    {"referencia": "<txid do Pix ou id_externo do link>",
     "valor_centavos": 10020,
     "pago_em": "2026-10-26T13:05:00-03:00"}

A assinatura é o HMAC-SHA256 do corpo bruto, em hexadecimal, no cabeçalho `X-Assinatura` (com ou
sem o prefixo `sha256=`), feita com o segredo do webhook. O log leva só a referência e o valor.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from agente.pagamento.confirmar import ResultadoConfirmacao

log = logging.getLogger(__name__)

CABECALHO_ASSINATURA = "X-Assinatura"

Confirmar = Callable[[Session, Any, str, int, datetime], ResultadoConfirmacao]


class PagamentoRecebido(BaseModel):
    referencia: str = Field(min_length=1, max_length=100)
    valor_centavos: int = Field(gt=0, strict=True)
    pago_em: datetime

    @field_validator("pago_em")
    @classmethod
    def _com_fuso(cls, valor: datetime) -> datetime:
        if valor.tzinfo is None:
            raise ValueError("pago_em precisa do fuso horário")
        return valor


def assinatura_valida(corpo: bytes, segredo: str, recebida: str | None) -> bool:
    if not recebida:
        return False
    recebida = recebida.removeprefix("sha256=")
    esperada = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(recebida.encode(), esperada.encode())


def criar_rota(
    caminho: str,
    origem: str,
    segredo: str | None,
    fabrica: sessionmaker[Session],
    dependencias: Any,
    confirmar: Callable[[], Confirmar],
) -> APIRouter:
    """A rota do webhook. `confirmar` é lido a cada chamada, para os testes poderem trocá-lo."""
    roteador = APIRouter()

    @roteador.post(caminho)
    async def webhook(request: Request) -> dict:
        if segredo is None or dependencias is None:
            raise HTTPException(503, f"o webhook {origem} está desligado")
        corpo = await request.body()
        if not assinatura_valida(corpo, segredo, request.headers.get(CABECALHO_ASSINATURA)):
            raise HTTPException(401, "assinatura inválida")
        try:
            pagamento = PagamentoRecebido.model_validate(json.loads(corpo))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(422, "o corpo não é um JSON válido") from None
        except ValidationError as erro:
            # Só o local do erro: o `input` do pydantic repetiria o corpo.
            campos = [".".join(str(parte) for parte in e["loc"]) for e in erro.errors()]
            raise HTTPException(422, f"payload inválido: {campos}") from None

        def _tratar() -> ResultadoConfirmacao:
            with fabrica() as sessao:
                resultado = confirmar()(
                    sessao,
                    dependencias,
                    pagamento.referencia,
                    pagamento.valor_centavos,
                    pagamento.pago_em,
                )
                sessao.commit()
                return resultado

        resultado = await run_in_threadpool(_tratar)
        if resultado is ResultadoConfirmacao.DESCONHECIDO:
            log.warning("webhook %s: referência desconhecida %s", origem, pagamento.referencia)
            raise HTTPException(404, "nenhum pagamento com essa referência")
        if resultado is ResultadoConfirmacao.VALOR_DIVERGENTE:
            log.warning(
                "webhook %s: valor divergente na referência %s (pago %s centavos)",
                origem,
                pagamento.referencia,
                pagamento.valor_centavos,
            )
        return {"resultado": resultado.value}

    return roteador
