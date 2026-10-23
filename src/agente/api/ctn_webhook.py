"""POST /webhooks/ctn: o CTN avisa que o link de pagamento foi pago (tarefa 6.3).

O payload e a assinatura são fictícios até a T9; estão em `webhook_pagamento`.
"""

from fastapi import APIRouter
from sqlalchemy.orm import Session, sessionmaker

from agente.api.webhook_pagamento import criar_rota
from agente.pagamento.concluir import DependenciasPagamento
from agente.pagamento.confirmar import confirmar_pagamento
from agente.tipos import FormaPagamento


def criar_rota_ctn(
    fabrica: sessionmaker[Session], segredo: str | None, dependencias: DependenciasPagamento | None
) -> APIRouter:
    return criar_rota(
        "/webhooks/ctn",
        "ctn",
        segredo,
        fabrica,
        dependencias,
        lambda: confirmar_pagamento,
        FormaPagamento.LINK_CTN,
    )
