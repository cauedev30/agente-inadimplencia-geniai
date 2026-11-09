"""O Pix que venceu sem pagamento (tarefa 6.6).

Vira expirado e pronto: não há novo contato com o cliente (G2.4). Se ele pedir de novo, o turno
gera um Pix novo.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca, Pagamento
from agente.tipos import FormaPagamento, StatusPagamento


def _expirar(
    sessao: Session, *condicoes, formas: tuple[FormaPagamento, ...] = (FormaPagamento.PIX,)
) -> int:
    """Expira os pagamentos pendentes (só os Pix, salvo `formas`) que batem com as condições,
    travando-os em ordem de id.

    O UPDATE confere o status no próprio comando: o webhook que grava PAGO entre a leitura e o
    commit não é sobrescrito por EXPIRADO.

    Ordem das travas: a varredura e o handoff só travam pagamentos, nunca a conversa nem a cobrança
    depois deles. Entre si, travam os Pix da mesma cobrança sempre pela mesma ordem (id), para dois
    UPDATEs de várias linhas não se cruzarem em ciclo. É a mesma trava (`NO KEY UPDATE`) da
    confirmação, que espera ou, se o pagamento mudou, deixa a linha de fora.
    """
    pendentes = (
        Pagamento.forma.in_(formas),
        Pagamento.status == StatusPagamento.PENDENTE,
        *condicoes,
    )
    ids = sessao.scalars(
        select(Pagamento.id)
        .where(*pendentes)
        .order_by(Pagamento.id)
        .with_for_update(key_share=True)
    ).all()
    if not ids:
        return 0
    resultado = sessao.execute(
        update(Pagamento)
        .where(Pagamento.id.in_(ids), Pagamento.status == StatusPagamento.PENDENTE)
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


def expirar_de_outro_valor(sessao: Session, cobranca: Cobranca) -> int:
    """Expira o Pix e o link pendentes cujo valor não é o da dívida de hoje da cobrança.

    É o abate por baixa externa: o cliente pagou parte por fora e a dívida diminuiu, então o Pix ou
    o link gerado com a dívida antiga, se pago, cobriria o mês pago por fora outra vez. Expirar
    tira o pagamento do reaproveitamento (`pix_vigente`) e o mantém na tabela, com o `br_code`,
    como histórico. Como no handoff, só vale no banco do agente: o Pix segue pagável no banco do
    CDT, e se for pago o pagamento ainda é confirmado (D39 cuida do que vier em dobro). As travas
    seguem a ordem de `db/travas.py`: quem chama já travou as conversas e a cobrança.
    """
    return _expirar(
        sessao,
        Pagamento.cobranca_id == cobranca.id,
        Pagamento.valor_centavos != cobranca.divida_centavos,
        formas=(FormaPagamento.PIX, FormaPagamento.LINK_CTN),
    )
