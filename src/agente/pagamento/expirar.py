"""O Pix que venceu sem pagamento (tarefa 6.6).

Vira expirado e pronto: não há novo contato com o cliente (G2.4). Se ele pedir de novo, o turno
gera um Pix novo.
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca, Pagamento
from agente.tipos import FormaPagamento, StatusPagamento


def _expirar(sessao: Session, *condicoes) -> int:
    """Um UPDATE só, com o status conferido no próprio comando: o webhook que grava PAGO entre a
    leitura e o commit não é sobrescrito por EXPIRADO.

    Ordem das travas: a varredura e o handoff só travam pagamentos, nunca a conversa nem a cobrança
    depois deles. Entre si, os dois UPDATEs de várias linhas podem pegar os Pix da mesma cobrança em
    ordens diferentes: só acontece com dois Pix pendentes nela, e o Postgres derruba um dos dois.
    """
    resultado = sessao.execute(
        update(Pagamento)
        .where(
            Pagamento.forma == FormaPagamento.PIX,
            Pagamento.status == StatusPagamento.PENDENTE,
            *condicoes,
        )
        .values(status=StatusPagamento.EXPIRADO)
        .execution_options(synchronize_session="fetch")
    )
    return resultado.rowcount


def expirar_pix(sessao: Session, agora: datetime) -> int:
    """Marca como expirados os Pix pendentes com o prazo vencido. Devolve quantos foram."""
    return _expirar(sessao, Pagamento.expira_em.is_not(None), Pagamento.expira_em < agora)


def expirar_pendentes(sessao: Session, cobranca: Cobranca) -> int:
    """Expira os Pix pendentes da cobrança, de uma vez (o handoff com `handoff_expira_pix`).

    Só expira no banco do agente: o Pix segue pagável no banco do CDT, e o pagamento ainda é
    confirmado (EXPIRADO entra no `confirmar_pagamento`). Cancelar a cobrança lá espera a T1.
    """
    return _expirar(sessao, Pagamento.cobranca_id == cobranca.id)
