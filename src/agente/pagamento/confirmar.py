"""Confirmação de pagamento (tarefa 6.1), em duas fases.

A 1ª fase, esta função, só mexe no banco e é a que faz o commit: o pagamento vira pago, a cobrança
paga, a conversa vai a PAGO e ENCERRADO, e a mensagem de confirmação fica gravada PENDENTE. A única
chamada externa é o link do convite do cartão, pedido antes das travas, e a falha dele só tira o
convite; uma queda do CTN ou do Chatwoot não perde o pagamento. A 2ª fase
(`pagamento/concluir.py`, uma rotina do Trabalhador) dá a baixa no CTN e envia a mensagem, com
tentativas. Quem chama decide o commit; a função só faz flush.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import BaixaCTNRegistro, Cliente, Cobranca, Conversa, Mensagem, Pagamento
from agente.db.travas import travar_conversas_e_cobranca
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
from agente.turno.gates import JANELA_WHATSAPP, humano_na_conversa

log = logging.getLogger(__name__)

# O motivo com que a rotina das baixas (`baixas_ctn.py`) fecha a cobrança quitada por fora.
MOTIVO_BAIXA_EXTERNA = "baixa_externa"
# Quanto o `pago_em` do nosso pagamento pode distar do da baixa do CTN para valerem como o mesmo
# pagamento. O `dt_baixa` pode vir só com a data (meia-noite UTC): menos que um dia não serve.
TOLERANCIA_DA_MESMA_BAIXA = timedelta(days=1)


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
    pagamento: Pagamento,
    conversa: Conversa | None,
    valor_centavos: int,
    agora: datetime,
) -> None:
    """O valor pago não bate: a conversa sai do robô, sem falar com o cliente.

    O valor avisado fica no pagamento (`divergente_centavos`), que o humano vê mesmo sem conversa,
    e a nota ao atendente sai pela rotina de conclusão, com tentativas.
    """
    pagamento.divergente_centavos = valor_centavos
    if conversa is None:
        log.warning("pagamento %s com valor divergente e sem conversa", pagamento.id)
    elif not (
        conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
        and conversa.motivo_humano is MotivoHumano.PAGAMENTO_DIVERGENTE
    ):  # o webhook repetido não repete a pendência
        transicionar(
            sessao,
            conversa,
            EstadoConversa.ATENDIMENTO_HUMANO,
            MotivoHumano.PAGAMENTO_DIVERGENTE,
            None,
            momento=agora,
        )
        if conversa.chatwoot_conversa_id is not None:
            pagamento.pendencias = [Pendencia.AVISO_DIVERGENTE.value]
            pagamento.proxima_tentativa_em = agora
    sessao.flush()


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


def _quer_convite(
    sessao: Session, pagamento: Pagamento, conversa: Conversa | None, agora: datetime
) -> bool:
    """O convite do cartão só vai na confirmação do Pix do propenso, dentro das 24h."""
    return (
        conversa is not None
        and pagamento.forma is FormaPagamento.PIX
        and conversa.cobranca.propenso_cartao
        and dentro_das_24h(sessao, conversa, agora)
    )


def _convite_antecipado(
    sessao: Session,
    deps: DependenciasConfirmar,
    pagamento: Pagamento,
    valor_centavos: int,
    agora: datetime,
) -> str | None:
    """O link do convite, pedido ao CTN antes de travar: o CTN lento não segura as travas.

    Lê sem trava e só como palpite do que a confirmação vai precisar: o que já está pago, o valor
    que não bate e a cobrança já paga não levam convite, então não chamam o CTN. A decisão de
    verdade é a `_gravar_confirmacao`, com tudo travado; o link que sobrar é descartado.
    """
    if pagamento.status is StatusPagamento.PAGO or valor_centavos != pagamento.valor_centavos:
        return None
    if pagamento.cobranca.status is StatusCobranca.PAGA:
        return None
    conversa = conversa_da_cobranca(pagamento)
    if not _quer_convite(sessao, pagamento, conversa, agora):
        return None
    return _link_do_convite(deps, pagamento.cobranca.cliente)


def _gravar_confirmacao(
    sessao: Session,
    deps: DependenciasConfirmar,
    pagamento: Pagamento,
    conversa: Conversa,
    agora: datetime,
    link_convite: str | None,
) -> None:
    """Grava a confirmação PENDENTE, fora do teto e da janela de atendimento (D33).

    Dentro das 24h do WhatsApp vai pelo Chatwoot (com o convite do cartão, se couber); fora delas,
    pelo template de utilidade, sem o convite. A rotina de conclusão envia.
    """
    cliente = conversa.cobranca.cliente
    valor_centavos = pagamento.valor_centavos
    if dentro_das_24h(sessao, conversa, agora):
        link_cartao = link_convite if _quer_convite(sessao, pagamento, conversa, agora) else None
        tipo = TipoFixa.CONFIRMACAO_CONVITE_CARTAO if link_cartao else TipoFixa.CONFIRMACAO
        dados = DadosFixa(
            nome=cliente.nome,
            valor_centavos=valor_centavos,
            link_cartao=link_cartao,
        )
        origem, texto = OrigemMensagem.FIXA, _texto_com_botoes(mensagem_fixa(tipo, dados))
    else:
        variaveis = variaveis_do_template(cliente, valor_centavos)
        origem = OrigemMensagem.ABERTURA
        texto = texto_do_template(deps.politica.template_confirmacao, variaveis)
    registrar_mensagem(
        sessao, conversa.id, origem, texto, None, False, agora, envio=EstadoEnvio.PENDENTE
    )


def _como_utc(instante: datetime) -> datetime:
    return instante if instante.tzinfo is not None else instante.replace(tzinfo=UTC)


def _baixa_do_proprio_pagamento(sessao: Session, pagamento: Pagamento, cobranca: Cobranca) -> bool:
    """A cobrança foi fechada pela rotina das baixas, e a baixa que a fechou é, tudo indica, a
    deste mesmo pagamento: o cliente pagou o nosso link ou Pix, o CTN deu a baixa e a rotina leu
    antes de o nosso webhook chegar. Não é pagamento em dobro (D39).

    Vale quando (a) o motivo do fechamento é `baixa_externa`, (b) nenhum outro pagamento nosso da
    cobrança está pago, e (c) alguma baixa do CTN aplicada a esta cobrança tem `pago_em` a menos de
    `TOLERANCIA_DA_MESMA_BAIXA` do pagamento. Um pagamento nosso dias depois do boleto continua
    sendo em dobro."""
    if cobranca.encerrada_motivo != MOTIVO_BAIXA_EXTERNA or pagamento.pago_em is None:
        return False
    if any(
        outro.id != pagamento.id and outro.status is StatusPagamento.PAGO
        for outro in cobranca.pagamentos
    ):
        return False
    pago_em = _como_utc(pagamento.pago_em)
    baixas_em = sessao.scalars(
        select(BaixaCTNRegistro.pago_em).where(BaixaCTNRegistro.cobranca_id == cobranca.id)
    )
    return any(abs(_como_utc(em) - pago_em) <= TOLERANCIA_DA_MESMA_BAIXA for em in baixas_em)


def _travar(sessao: Session, pagamento: Pagamento) -> Pagamento:
    """Trava as conversas, a cobrança e o pagamento, nessa ordem, e os relê já travados.

    O Postgres lê em READ COMMITTED: dois webhooks iguais em paralelo leriam PENDENTE e mandariam
    duas confirmações, e o pagamento do Pix e o do link da mesma cobrança dariam duas baixas. O 2º
    espera na trava e lê PAGO.

    A ordem é a mesma do turno ativo, que segura a conversa desde o início (`tomar_turno`) e só
    depois relê a cobrança: conversa, cobrança, pagamento, nos dois lados, para as transações
    esperarem uma pela outra sem ciclo. A trava é a de `NO KEY UPDATE`, que não barra o
    `FOR KEY SHARE` das chaves estrangeiras (a mensagem que a rotina de conclusão grava).
    """
    travar_conversas_e_cobranca(sessao, pagamento.cobranca_id)
    return sessao.scalars(
        select(Pagamento)
        .where(Pagamento.id == pagamento.id)
        .with_for_update(key_share=True)
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
    agora = _agora()
    convite = _convite_antecipado(sessao, deps, pagamento, valor_centavos, agora)
    pagamento = _travar(sessao, pagamento)
    if pagamento.status is StatusPagamento.PAGO:
        return ResultadoConfirmacao.JA_CONFIRMADO
    conversa = conversa_da_cobranca(pagamento)
    if valor_centavos != pagamento.valor_centavos:
        _pendencia_de_valor(sessao, pagamento, conversa, valor_centavos, agora)
        return ResultadoConfirmacao.VALOR_DIVERGENTE

    cobranca = pagamento.cobranca
    pagamento.status = StatusPagamento.PAGO
    pagamento.pago_em = pago_em
    if cobranca.status is StatusCobranca.PAGA and _baixa_do_proprio_pagamento(
        sessao, pagamento, cobranca
    ):
        # O CTN deu a baixa deste pagamento e a rotina das baixas a leu antes do webhook: a cobrança
        # já está fechada e o cliente já foi avisado. Fica pago, sem nova baixa, mensagem ou aviso.
        log.info("o pagamento %s é a baixa que fechou a cobrança %s", pagamento.id, cobranca.id)
        pagamento.pendencias = []
        pagamento.proxima_tentativa_em = None
        sessao.flush()
        return ResultadoConfirmacao.CONFIRMADO
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
        com_humano = humano_na_conversa(conversa)
        transicionar(sessao, conversa, EstadoConversa.PAGO, None, None, momento=agora)
        _gravar_confirmacao(sessao, deps, pagamento, conversa, agora, convite)
        pendencias.append(Pendencia.MENSAGEM.value)
        if com_humano and conversa.chatwoot_conversa_id is not None:
            pendencias.append(Pendencia.AVISO_HUMANO.value)
        transicionar(sessao, conversa, EstadoConversa.ENCERRADO, None, None, momento=agora)
    pagamento.pendencias = pendencias
    pagamento.proxima_tentativa_em = agora if pendencias else None
    sessao.flush()
    return ResultadoConfirmacao.CONFIRMADO
