"""POST /webhooks/pix: o banco avisa que o Pix foi pago (tarefa 6.3).

O payload e a assinatura são fictícios até a T1; estão em `webhook_pagamento`.
"""

from fastapi import APIRouter
from sqlalchemy.orm import Session, sessionmaker

from agente.api.webhook_pagamento import criar_rota
from agente.pagamento.concluir import DependenciasPagamento
from agente.pagamento.confirmar import confirmar_pagamento
from agente.tipos import FormaPagamento


def criar_rota_pix(
    fabrica: sessionmaker[Session], segredo: str | None, dependencias: DependenciasPagamento | None
) -> APIRouter:
    return criar_rota(
        "/webhooks/pix",
        "pix",
        segredo,
        fabrica,
        dependencias,
        lambda: confirmar_pagamento,
        FormaPagamento.PIX,
    )
