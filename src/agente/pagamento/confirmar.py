"""Confirmação de pagamento (tarefa 6.1), em duas fases.

A 1ª fase, esta função, só mexe no banco e é a que faz o commit: o pagamento vira pago, a cobrança
paga, a conversa vai a PAGO e ENCERRADO, e a mensagem de confirmação fica gravada PENDENTE. Nada
externo roda aqui, então uma queda do CTN ou do Chatwoot não perde o pagamento. A 2ª fase
(`pagamento/concluir.py`, uma rotina do Trabalhador) dá a baixa no CTN e envia a mensagem, com
tentativas. Quem chama decide o commit; a função só faz flush.
"""

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import Cliente, Cobranca, Conversa, Mensagem, Pagamento
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.integracoes.disparador import Disparador
from agente.politica import Politica
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    FormaPagamento,
    MotivoHumano,
    OrigemMensagem,
    Pendencia,
    StatusCobranca,
    StatusPagamento,
    TipoFixa,
)
from agente.turno.catalogo import DadosFixa, MensagemPronta, mensagem_fixa
from agente.turno.gates import JANELA_WHATSAPP

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


def conversa_da_cobranca(pagamento: Pagamento) -> Conversa | None:
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


def dentro_das_24h(sessao: Session, conversa: Conversa, agora: datetime) -> bool:
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


def variaveis_do_template(cliente: Cliente, valor_centavos: int) -> dict[str, str]:
    return {
        "nome": cliente.nome.split()[0] if cliente.nome.strip() else "",
        "valor": formatar_reais(valor_centavos),
    }


def texto_do_template(template: str, variaveis: dict[str, str]) -> str:
    """Como o template fica no histórico: o disparador não devolve o texto."""
    return f"[template {template}] {json.dumps(variaveis, ensure_ascii=False)}"


def _texto_com_botoes(mensagem: MensagemPronta) -> str:
    """O botão de link vai no texto, como o Chatwoot o entrega: o texto gravado é o que sai."""
    links = [f"{botao.texto}: {botao.valor}" for botao in mensagem.botoes if botao.tipo == "url"]
    if not links:
        return mensagem.texto
    return f"{mensagem.texto}\n\n" + "\n".join(links)


def _link_do_convite(deps: DependenciasConfirmar, cliente: Cliente) -> str | None:
    """O convite do cartão é acessório: se o CTN falha, a confirmação sai simples."""
    try:
        return deps.ctn.gerar_link_cadastro_cartao(cliente.matricula_ctn).url
    except (CTNIndisponivel, ClienteNaoEncontrado, httpx.HTTPError):
        log.warning("o link do convite do cartão falhou; a confirmação sai sem o convite")
        return None


def _gravar_confirmacao(
    sessao: Session,
    deps: DependenciasConfirmar,
    pagamento: Pagamento,
    conversa: Conversa,
    agora: datetime,
) -> None:
    """Grava a confirmação PENDENTE, fora do teto e da janela de atendimento (D33).

    Dentro das 24h do WhatsApp vai pelo Chatwoot (com o convite do cartão, se couber); fora delas,
    pelo template de utilidade, sem o convite. A rotina de conclusão envia.
    """
    cliente = conversa.cobranca.cliente
    valor_centavos = pagamento.valor_centavos
    if dentro_das_24h(sessao, conversa, agora):
        convite = pagamento.forma is FormaPagamento.PIX and conversa.cobranca.propenso_cartao
        link_cartao = _link_do_convite(deps, cliente) if convite else None
        tipo = TipoFixa.CONFIRMACAO_CONVITE_CARTAO if link_cartao else TipoFixa.CONFIRMACAO
        dados = DadosFixa(
            nome=cliente.nome,
            valor_centavos=valor_centavos,
            link_cartao=link_cartao,
            link_canal_externo=deps.politica.link_canal_externo,
        )
        origem, texto = OrigemMensagem.FIXA, _texto_com_botoes(mensagem_fixa(tipo, dados))
    else:
        variaveis = variaveis_do_template(cliente, valor_centavos)
        origem = OrigemMensagem.ABERTURA
        texto = texto_do_template(deps.politica.template_confirmacao, variaveis)
    registrar_mensagem(
        sessao, conversa.id, origem, texto, None, False, agora, envio=EstadoEnvio.PENDENTE
    )


def _travar(sessao: Session, pagamento: Pagamento) -> Pagamento:
    """Trava a cobrança e o pagamento e os relê já travados, para a confirmação não correr em dobro.

    O Postgres lê em READ COMMITTED: dois webhooks iguais em paralelo leriam PENDENTE e mandariam
    duas confirmações, e o pagamento do Pix e o do link da mesma cobrança dariam duas baixas. O 2º
    espera na trava e lê PAGO. A cobrança vem primeiro, na mesma ordem do turno (que escreve nela
    antes de tocar nos pagamentos), para as duas transações não se travarem uma à outra.
    """
    sessao.execute(
        select(Cobranca)
        .where(Cobranca.id == pagamento.cobranca_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return sessao.scalars(
        select(Pagamento)
        .where(Pagamento.id == pagamento.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def confirmar_pagamento(
    sessao: Session,
    deps: DependenciasConfirmar,
    referencia: str,
    valor_centavos: int,
    pago_em: datetime,
) -> ResultadoConfirmacao:
    """Confirma o pagamento da referência (o txid do Pix ou o id do link do CTN). É idempotente.

    A 1ª fase: o pagamento vira pago, a cobrança paga e a conversa vai a PAGO e a ENCERRADO; a
    mensagem de confirmação fica gravada PENDENTE e a baixa no CTN (sempre no Pix; no link,
    conforme `agente_da_baixa_no_link`) fica como pendência. A rotina de conclusão faz o resto.
    A conversa com o humano também confirma e sai. Os outros pagamentos pendentes da cobrança ficam
    como estão. O segundo pagamento de uma cobrança já paga (D39) é marcado como pago, sem nova
    baixa nem mensagem, e deixa o aviso ao atendente como pendência.
    """
    pagamento = sessao.scalar(select(Pagamento).where(Pagamento.referencia == referencia))
    if pagamento is None:
        return ResultadoConfirmacao.DESCONHECIDO
    pagamento = _travar(sessao, pagamento)
    if pagamento.status is StatusPagamento.PAGO:
        return ResultadoConfirmacao.JA_CONFIRMADO
    agora = _agora()
    conversa = conversa_da_cobranca(pagamento)
    if valor_centavos != pagamento.valor_centavos:
        _pendencia_de_valor(sessao, deps, pagamento, conversa, valor_centavos, agora)
        return ResultadoConfirmacao.VALOR_DIVERGENTE

    cobranca = pagamento.cobranca
    pagamento.status = StatusPagamento.PAGO
    pagamento.pago_em = pago_em
    if cobranca.status is StatusCobranca.PAGA:
        # Pagamento em dobro (D39): fica pago, sem nova baixa nem mensagem, e o atendente é avisado.
        log.warning("segundo pagamento (%s) da cobrança %s já paga", pagamento.id, cobranca.id)
        pagamento.pendencias = [Pendencia.AVISO_DUPLICADO.value]
        pagamento.proxima_tentativa_em = agora
        sessao.flush()
        return ResultadoConfirmacao.CONFIRMADO
    cobranca.status = StatusCobranca.PAGA
    pendencias = []
    if pagamento.forma is FormaPagamento.PIX or deps.politica.agente_da_baixa_no_link:
        pendencias.append(Pendencia.BAIXA.value)
    if conversa is not None:
        transicionar(sessao, conversa, EstadoConversa.PAGO, None, None, momento=agora)
        _gravar_confirmacao(sessao, deps, pagamento, conversa, agora)
        pendencias.append(Pendencia.MENSAGEM.value)
        transicionar(sessao, conversa, EstadoConversa.ENCERRADO, None, None, momento=agora)
    pagamento.pendencias = pendencias
    pagamento.proxima_tentativa_em = agora if pendencias else None
    sessao.flush()
    return ResultadoConfirmacao.CONFIRMADO
