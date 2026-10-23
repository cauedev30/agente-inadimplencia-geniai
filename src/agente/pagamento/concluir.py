"""A 2ª fase da confirmação do pagamento (tarefa 6.1): o que depende do CTN e do Chatwoot.

A 1ª fase (`confirmar_pagamento`) grava o pagamento como pago, a mensagem PENDENTE e as
pendências, e faz o commit. Esta rotina do Trabalhador refaz cada pendência até ela dar certo, com
a espera crescente da integração (`espera_integracao`): a baixa no CTN, que é idempotente pela
referência, e o envio da confirmação, que confere o Chatwoot antes de reenviar. Uma falha numa
pendência não trava as outras, e uma queda do CTN ou do Chatwoot nunca desfaz o pagamento.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Mensagem, Pagamento
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.integracoes.disparador import Disparador, FalhaNoEnvio
from agente.pagamento.confirmar import (
    conversa_da_cobranca,
    dentro_das_24h,
    texto_do_template,
    variaveis_do_template,
)
from agente.politica import Politica
from agente.tipos import EstadoEnvio, OrigemMensagem, Pendencia, StatusPagamento
from agente.turno.entrega import confirmar_envio, entregue

log = logging.getLogger(__name__)

_ETIQUETA_DUPLICADO = "pagamento_duplicado"

# O que conta como integração fora do ar: fica para a próxima tentativa. Qualquer outro erro é bug
# de código: também fica para a próxima, mas com o stack trace no log.
_FALHAS_DE_INTEGRACAO = (httpx.HTTPError, ClienteNaoEncontrado, CTNIndisponivel, FalhaNoEnvio)


class DependenciasConcluir(Protocol):
    politica: Politica
    chatwoot: Chatwoot
    disparador: Disparador
    time_humano: int | None
    ctn: CTN


def _dar_baixa(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    cliente = pagamento.cobranca.cliente
    deps.ctn.registrar_baixa(
        cliente.matricula_ctn, pagamento.valor_centavos, pagamento.pago_em, pagamento.referencia
    )
    pagamento.baixa_ctn_em = agora


def _confirmacao_pendente(sessao: Session, pagamento: Pagamento) -> Mensagem | None:
    conversas = [conversa.id for conversa in pagamento.cobranca.conversas]
    return sessao.scalars(
        select(Mensagem)
        .where(
            Mensagem.conversa_id.in_(conversas),
            Mensagem.envio == EstadoEnvio.PENDENTE,
            Mensagem.origem.in_((OrigemMensagem.FIXA, OrigemMensagem.ABERTURA)),
        )
        .order_by(Mensagem.id)
    ).first()


def _enviar_confirmacao(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """Envia a confirmação gravada PENDENTE; o que já saiu no Chatwoot não sai de novo."""
    mensagem = _confirmacao_pendente(sessao, pagamento)
    if mensagem is None:
        return
    conversa = mensagem.conversa
    cliente = conversa.cobranca.cliente
    template = deps.politica.template_confirmacao
    variaveis = variaveis_do_template(cliente, pagamento.valor_centavos)
    if mensagem.origem is OrigemMensagem.FIXA and not dentro_das_24h(sessao, conversa, agora):
        # A janela fechou na espera: só o template pode sair (D33).
        mensagem.origem = OrigemMensagem.ABERTURA
        mensagem.texto = texto_do_template(template, variaveis)
        sessao.flush()
    if mensagem.origem is OrigemMensagem.FIXA:
        chatwoot_id = entregue(sessao, conversa, mensagem, deps.chatwoot)
        if chatwoot_id is None:
            chatwoot_id = deps.chatwoot.enviar_mensagem(
                conversa.chatwoot_conversa_id, mensagem.texto
            )
        confirmar_envio(sessao, mensagem, chatwoot_id)
        return
    deps.disparador.enviar_template(cliente.telefone, template, variaveis, [])
    mensagem.envio = EstadoEnvio.ENVIADA
    sessao.flush()


def _avisar_duplicado(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """O 2º pagamento da cobrança (D39): nota privada e etiqueta, para o atendente devolver.

    A conversa fica como está (ENCERRADO). O Chatwoot reabre e vai para o time, senão a nota
    passa despercebida numa conversa resolvida. A nota vai por último: se algo falha antes, ela não
    se repete.
    """
    conversa = conversa_da_cobranca(pagamento)
    if conversa is None or conversa.chatwoot_conversa_id is None:
        log.warning("pagamento %s em dobro sem conversa no Chatwoot: conferir à mão", pagamento.id)
        return
    chatwoot_id = conversa.chatwoot_conversa_id
    pagos = sorted(
        (p for p in pagamento.cobranca.pagamentos if p.status is StatusPagamento.PAGO),
        key=lambda p: (p.id == pagamento.id, p.pago_em, p.id),
    )
    linhas = ["Pagamento em dobro: a cobrança já estava paga quando este chegou."]
    for outro in pagos:
        quando = "agora" if outro.id == pagamento.id else "antes"
        linhas.append(
            f"- pago {quando}: referência {outro.referencia} ({outro.forma.value}), "
            f"{formatar_reais(outro.valor_centavos)}"
        )
    linhas.append("Falta devolver o valor pago a mais.")
    deps.chatwoot.etiquetar(chatwoot_id, [_ETIQUETA_DUPLICADO])
    if deps.time_humano is not None:
        deps.chatwoot.atribuir(chatwoot_id, deps.time_humano)
    deps.chatwoot.mudar_status(chatwoot_id, "open")
    deps.chatwoot.nota_privada(chatwoot_id, "\n".join(linhas))


_PASSOS: dict[Pendencia, Callable[[Session, DependenciasConcluir, Pagamento, datetime], None]] = {
    Pendencia.BAIXA: _dar_baixa,
    Pendencia.MENSAGEM: _enviar_confirmacao,
    Pendencia.AVISO_DUPLICADO: _avisar_duplicado,
}


def _concluir(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    restantes = []
    for nome in pagamento.pendencias:
        try:
            with sessao.begin_nested():
                _PASSOS[Pendencia(nome)](sessao, deps, pagamento, agora)
        except _FALHAS_DE_INTEGRACAO as erro:
            log.warning("pagamento %s: %s falhou (%s)", pagamento.id, nome, type(erro).__name__)
            restantes.append(nome)
        except Exception:
            log.exception("pagamento %s: %s falhou por erro de código", pagamento.id, nome)
            restantes.append(nome)
    pagamento.pendencias = restantes
    if not restantes:
        pagamento.proxima_tentativa_em = None
        return
    pagamento.tentativas_conclusao += 1
    pagamento.proxima_tentativa_em = agora + deps.politica.espera_integracao(
        pagamento.tentativas_conclusao
    )
    if pagamento.tentativas_conclusao >= deps.politica.integracao_tentativas:
        log.error(
            "pagamento %s pago, mas sem concluir %s depois de %s tentativas: conferir à mão",
            pagamento.id,
            restantes,
            pagamento.tentativas_conclusao,
        )


def concluir_pagamentos(sessao: Session, deps: DependenciasConcluir, agora: datetime) -> int:
    """Conclui as pendências vencidas dos pagamentos confirmados. Devolve quantos pagamentos tratou.

    Cada pagamento é commitado sozinho, para o que já saiu não se perder se o seguinte quebrar, e é
    travado (pulando o que outra instância já pegou), para a baixa e a mensagem não saírem duas
    vezes.
    """
    vencidos = sessao.scalars(
        select(Pagamento.id)
        .where(Pagamento.proxima_tentativa_em.is_not(None), Pagamento.proxima_tentativa_em <= agora)
        .order_by(Pagamento.proxima_tentativa_em, Pagamento.id)
    ).all()
    tratados = 0
    for pagamento_id in vencidos:
        pagamento = sessao.scalar(
            select(Pagamento)
            .where(Pagamento.id == pagamento_id, Pagamento.proxima_tentativa_em.is_not(None))
            .with_for_update(skip_locked=True)
        )
        if pagamento is None:
            continue
        _concluir(sessao, deps, pagamento, agora)
        sessao.commit()
        tratados += 1
    return tratados
