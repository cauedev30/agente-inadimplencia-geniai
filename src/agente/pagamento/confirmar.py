"""Confirmação de pagamento (tarefa 6.1).

Contrato fixado antes da etapa 6 para os webhooks (6.3) poderem chamar a função enquanto a 6.1 é
implementada em paralelo.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session


class ResultadoConfirmacao(StrEnum):
    CONFIRMADO = "CONFIRMADO"
    # O webhook repetido: o pagamento já estava pago, nada sai de novo.
    JA_CONFIRMADO = "JA_CONFIRMADO"
    # O valor pago não bate com o do pagamento: vira pendência para o humano, sem confirmar.
    VALOR_DIVERGENTE = "VALOR_DIVERGENTE"
    # Nenhum pagamento com essa referência.
    DESCONHECIDO = "DESCONHECIDO"


def confirmar_pagamento(
    sessao: Session, deps: Any, referencia: str, valor_centavos: int, pago_em: datetime
) -> ResultadoConfirmacao:
    """Confirma o pagamento da referência (o txid do Pix ou o id do link do CTN). É idempotente."""
    raise NotImplementedError
