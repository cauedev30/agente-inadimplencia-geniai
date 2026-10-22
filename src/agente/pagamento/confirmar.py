"""Confirmação de pagamento (tarefa 6.1).

A assinatura foi fixada antes da etapa 6 para os webhooks (6.3) chamarem a função enquanto ela era
implementada. Quem chama decide o commit; a função só faz flush. Se a baixa ou o envio falharem, a
exceção sobe e o webhook é refeito: a baixa é idempotente pela referência.
"""

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import Conversa, Mensagem, Pagamento
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN
from agente.integracoes.disparador import Disparador
from agente.politica import Politica
from agente.tipos import (
    EstadoConversa,
    FormaPagamento,
    MotivoHumano,
    OrigemMensagem,
    StatusCobranca,
    StatusPagamento,
    TipoFixa,
)
from agente.turno.catalogo import DadosFixa
from agente.turno.gates import JANELA_WHATSAPP
from agente.turno.handoff import enviar_fixa

log = logging.getLogger(__name__)


class DependenciasConfirmar(Protocol):
    politica: Politica
    chatwoot: Chatwoot
    disparador: Disparador
    time_humano: int | None
    ctn: CTN


class ResultadoConfirmacao(StrEnum):
    CONFIRMADO = "CONFIRMADO"
    # O webhook repetido: o pagamento já estava pago, nada sai de novo.
    JA_CONFIRMADO = "JA_CONFIRMADO"
    # O valor pago não bate com o do pagamento: vira pendência para o humano, sem confirmar.
    VALOR_DIVERGENTE = "VALOR_DIVERGENTE"
    # Nenhum pagamento com essa referência.
    DESCONHECIDO = "DESCONHECIDO"


def _agora() -> datetime:
    return datetime.now(UTC)


def _conversa_da_cobranca(pagamento: Pagamento) -> Conversa | None:
    """A conversa mais recente da cobrança (o reinício abre uma nova para a mesma cobrança)."""
    conversas = pagamento.cobranca.conversas
    return max(conversas, key=lambda conversa: conversa.id) if conversas else None


def _pendencia_de_valor(
    sessao: Session,
    deps: DependenciasConfirmar,
    pagamento: Pagamento,
    conversa: Conversa | None,
    valor_centavos: int,
    agora: datetime,
) -> None:
    """O valor pago não bate: nota ao humano e a conversa sai do robô, sem falar com o cliente."""
    if conversa is None:
        log.warning("pagamento %s com valor divergente e sem conversa", pagamento.id)
        return
    if (
        conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
        and conversa.motivo_humano is MotivoHumano.PAGAMENTO_DIVERGENTE
    ):
        return  # o webhook repetido
    motivo = MotivoHumano.PAGAMENTO_DIVERGENTE
    chatwoot_id = conversa.chatwoot_conversa_id
    if chatwoot_id is not None:
        nota = (
            f"Saída para o humano: {motivo.value}\n"
            f"- referência {pagamento.referencia} ({pagamento.forma.value}): "
            f"pago {formatar_reais(valor_centavos)}, esperado "
            f"{formatar_reais(pagamento.valor_centavos)}"
        )
        deps.chatwoot.nota_privada(chatwoot_id, nota)
        deps.chatwoot.etiquetar(chatwoot_id, [motivo.value.lower()])
        if deps.time_humano is not None:
            deps.chatwoot.atribuir(chatwoot_id, deps.time_humano)
        deps.chatwoot.mudar_status(chatwoot_id, "open")
    transicionar(sessao, conversa, EstadoConversa.ATENDIMENTO_HUMANO, motivo, None, momento=agora)


def _dentro_das_24h(sessao: Session, conversa: Conversa, agora: datetime) -> bool:
    ultima = sessao.scalar(
        select(func.max(Mensagem.momento)).where(
            Mensagem.conversa_id == conversa.id, Mensagem.origem == OrigemMensagem.CLIENTE
        )
    )
    return (
        conversa.chatwoot_conversa_id is not None
        and ultima is not None
        and agora - ultima <= JANELA_WHATSAPP
    )


def _avisar(
    sessao: Session,
    deps: DependenciasConfirmar,
    pagamento: Pagamento,
    conversa: Conversa,
    valor_centavos: int,
    agora: datetime,
) -> None:
    """A confirmação sai na hora, fora da janela de atendimento e do teto (D33).

    Dentro das 24h do WhatsApp vai pelo Chatwoot (com o convite do cartão, se couber); fora delas,
    pelo template de utilidade, sem o convite.
    """
    cliente = conversa.cobranca.cliente
    if _dentro_das_24h(sessao, conversa, agora):
        convite = pagamento.forma is FormaPagamento.PIX and conversa.cobranca.propenso_cartao
        link_cartao = (
            deps.ctn.gerar_link_cadastro_cartao(cliente.matricula_ctn).url if convite else None
        )
        tipo = TipoFixa.CONFIRMACAO_CONVITE_CARTAO if convite else TipoFixa.CONFIRMACAO
        dados = DadosFixa(
            nome=cliente.nome,
            valor_centavos=valor_centavos,
            link_cartao=link_cartao,
            link_canal_externo=deps.politica.link_canal_externo,
        )
        enviar_fixa(sessao, deps.chatwoot, conversa, tipo, dados, agora)
        return
    template = deps.politica.template_confirmacao
    variaveis = {
        "nome": cliente.nome.split()[0] if cliente.nome.strip() else "",
        "valor": formatar_reais(valor_centavos),
    }
    deps.disparador.enviar_template(cliente.telefone, template, variaveis, [])
    texto = f"[template {template}] {json.dumps(variaveis, ensure_ascii=False)}"
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.ABERTURA, texto, None, False, agora)


def confirmar_pagamento(
    sessao: Session,
    deps: DependenciasConfirmar,
    referencia: str,
    valor_centavos: int,
    pago_em: datetime,
) -> ResultadoConfirmacao:
    """Confirma o pagamento da referência (o txid do Pix ou o id do link do CTN). É idempotente.

    A sequência: o pagamento vira pago e a conversa vai para PAGO; sai a baixa no CTN (sempre no Pix;
    no link, conforme `ctn_baixa_no_link`); sai a mensagem fixa; a conversa vai para ENCERRADO. A
    conversa com o humano também confirma e sai. Os outros pagamentos pendentes da cobrança ficam
    como estão. O segundo pagamento de uma cobrança já paga só é marcado como pago: não repete a
    baixa nem a mensagem, e o aviso no log fica para a conferência humana.
    """
    pagamento = sessao.scalar(select(Pagamento).where(Pagamento.referencia == referencia))
    if pagamento is None:
        return ResultadoConfirmacao.DESCONHECIDO
    if pagamento.status is StatusPagamento.PAGO:
        return ResultadoConfirmacao.JA_CONFIRMADO
    agora = _agora()
    conversa = _conversa_da_cobranca(pagamento)
    if valor_centavos != pagamento.valor_centavos:
        _pendencia_de_valor(sessao, deps, pagamento, conversa, valor_centavos, agora)
        return ResultadoConfirmacao.VALOR_DIVERGENTE

    cobranca = pagamento.cobranca
    pagamento.status = StatusPagamento.PAGO
    pagamento.pago_em = pago_em
    if cobranca.status is StatusCobranca.PAGA:
        log.warning("segundo pagamento (%s) da cobrança %s já paga", pagamento.id, cobranca.id)
        sessao.flush()
        return ResultadoConfirmacao.CONFIRMADO
    cobranca.status = StatusCobranca.PAGA
    if conversa is not None:
        transicionar(sessao, conversa, EstadoConversa.PAGO, None, None, momento=agora)

    if pagamento.forma is FormaPagamento.PIX or deps.politica.ctn_baixa_no_link:
        deps.ctn.registrar_baixa(
            cobranca.cliente.matricula_ctn, valor_centavos, pago_em, pagamento.referencia
        )
        pagamento.baixa_ctn_em = agora

    if conversa is not None:
        _avisar(sessao, deps, pagamento, conversa, valor_centavos, agora)
        transicionar(sessao, conversa, EstadoConversa.ENCERRADO, None, None, momento=agora)
    sessao.flush()
    return ResultadoConfirmacao.CONFIRMADO
