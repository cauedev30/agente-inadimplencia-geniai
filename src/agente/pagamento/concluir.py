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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.db.modelos import Mensagem, Pagamento
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.integracoes.disparador import Disparador, FalhaNoEnvio
from agente.pagamento.confirmar import (
    DependenciasConfirmar,
    conversa_da_cobranca,
    dentro_das_24h,
    texto_do_template,
    variaveis_do_template,
)
from agente.politica import Politica
from agente.tipos import EstadoEnvio, MotivoHumano, OrigemMensagem, Pendencia, StatusPagamento
from agente.turno.entrega import confirmar_envio, entregue

log = logging.getLogger(__name__)

_ETIQUETA_DUPLICADO = "pagamento_duplicado"
_ETIQUETA_ESGOTADO = "pagamento_sem_concluir"

# O que conta como integração fora do ar: fica para a próxima tentativa. Qualquer outro erro é bug
# de código: também fica para a próxima, mas com o stack trace no log.
_FALHAS_DE_INTEGRACAO = (httpx.HTTPError, ClienteNaoEncontrado, CTNIndisponivel, FalhaNoEnvio)


class DependenciasConcluir(Protocol):
    politica: Politica
    chatwoot: Chatwoot
    disparador: Disparador
    time_humano: int | None
    ctn: CTN


class DependenciasPagamento(DependenciasConfirmar, DependenciasConcluir, Protocol):
    """O que o serviço de pagamentos precisa: as dependências das duas fases da confirmação."""


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


def _avisar_divergente(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """O valor pago não bate: o atendente recebe a nota e a etiqueta (a conversa já saiu do robô).

    A nota vai por último, para não se repetir se uma chamada anterior falhar.
    """
    conversa = conversa_da_cobranca(pagamento)
    if conversa is None or conversa.chatwoot_conversa_id is None:
        log.warning(
            "pagamento %s divergente sem conversa no Chatwoot: conferir à mão", pagamento.id
        )
        return
    chatwoot_id = conversa.chatwoot_conversa_id
    motivo = MotivoHumano.PAGAMENTO_DIVERGENTE
    nota = (
        f"Saída para o humano: {motivo.value}\n"
        f"- referência {pagamento.referencia} ({pagamento.forma.value}): "
        f"pago {formatar_reais(pagamento.divergente_centavos)}, esperado "
        f"{formatar_reais(pagamento.valor_centavos)}"
    )
    deps.chatwoot.etiquetar(chatwoot_id, [motivo.value.lower()])
    if deps.time_humano is not None:
        deps.chatwoot.atribuir(chatwoot_id, deps.time_humano)
    deps.chatwoot.mudar_status(chatwoot_id, "open")
    deps.chatwoot.nota_privada(chatwoot_id, nota)


def _avisar_humano(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """O pagamento confirmou com um atendente na conversa: ele soube pela nota, não pelo cliente."""
    conversa = conversa_da_cobranca(pagamento)
    if conversa is None or conversa.chatwoot_conversa_id is None:
        return
    deps.chatwoot.nota_privada(
        conversa.chatwoot_conversa_id,
        f"Pagamento confirmado: referência {pagamento.referencia} ({pagamento.forma.value}), "
        f"{formatar_reais(pagamento.valor_centavos)}. O cliente recebeu a confirmação e o robô "
        "encerrou a conversa.",
    )


def _avisar_esgotado(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """As tentativas se esgotaram: a conversa reabre com nota e etiqueta, como no pagamento dobrado.

    O pagamento fica contado no /saude (`atencao_em`) até a conclusão sair. A nota vai por
    último.
    """
    pagamento.atencao_em = agora
    conversa = conversa_da_cobranca(pagamento)
    if conversa is None or conversa.chatwoot_conversa_id is None:
        log.warning("pagamento %s pago e sem concluir, sem conversa: conferir à mão", pagamento.id)
        return
    chatwoot_id = conversa.chatwoot_conversa_id
    faltando = [
        nome
        for nome in pagamento.pendencias
        if nome not in (Pendencia.AVISO_ESGOTADO.value, Pendencia.AVISO_CONCLUIDO.value)
    ]
    deps.chatwoot.etiquetar(chatwoot_id, [_ETIQUETA_ESGOTADO])
    if deps.time_humano is not None:
        deps.chatwoot.atribuir(chatwoot_id, deps.time_humano)
    deps.chatwoot.mudar_status(chatwoot_id, "open")
    deps.chatwoot.nota_privada(
        chatwoot_id,
        f"Pagamento confirmado, mas sem concluir depois de {pagamento.tentativas_conclusao} "
        f"tentativas: referência {pagamento.referencia} ({pagamento.forma.value}), "
        f"{formatar_reais(pagamento.valor_centavos)}. Falta: {', '.join(faltando) or '-'} "
        "(BAIXA é a baixa no CTN; MENSAGEM é a confirmação ao cliente). O robô segue tentando.",
    )


def _resolver_webchat(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """A cobrança foi paga: a conversa do webchat amarrada a ela é resolvida no Chatwoot.

    "Fechada" hoje significa "paga": o encerramento sem pagamento é dúvida aberta (G7).
    """
    webchat_id = pagamento.cobranca.webchat_conversa_id
    if webchat_id is not None:
        deps.chatwoot.mudar_status(webchat_id, "resolved")


def _avisar_concluido(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """A conclusão saiu depois do aviso de esgotado: o atendente soube que faltava, vê que acabou.

    Só a nota: a conversa fica com ele, que a fecha. A etiqueta do aviso permanece como histórico.
    """
    conversa = conversa_da_cobranca(pagamento)
    if conversa is None or conversa.chatwoot_conversa_id is None:
        return
    deps.chatwoot.nota_privada(
        conversa.chatwoot_conversa_id,
        f"Pagamento concluído: referência {pagamento.referencia} ({pagamento.forma.value}), "
        f"{formatar_reais(pagamento.valor_centavos)}. A baixa no CTN e a confirmação ao cliente "
        "saíram; não falta mais nada.",
    )


_PASSOS: dict[Pendencia, Callable[[Session, DependenciasConcluir, Pagamento, datetime], None]] = {
    Pendencia.BAIXA: _dar_baixa,
    Pendencia.MENSAGEM: _enviar_confirmacao,
    Pendencia.AVISO_DUPLICADO: _avisar_duplicado,
    Pendencia.AVISO_DIVERGENTE: _avisar_divergente,
    Pendencia.AVISO_HUMANO: _avisar_humano,
    Pendencia.AVISO_ESGOTADO: _avisar_esgotado,
    Pendencia.AVISO_CONCLUIDO: _avisar_concluido,
    Pendencia.RESOLVER_WEBCHAT: _resolver_webchat,
}


def _passo(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, nome: str, agora: datetime
) -> bool:
    """Roda uma pendência num savepoint. Devolve False se falhou: ela fica para a próxima."""
    try:
        with sessao.begin_nested():
            _PASSOS[Pendencia(nome)](sessao, deps, pagamento, agora)
    except _FALHAS_DE_INTEGRACAO as erro:
        log.warning("pagamento %s: %s falhou (%s)", pagamento.id, nome, type(erro).__name__)
        return False
    except Exception:
        log.exception("pagamento %s: %s falhou por erro de código", pagamento.id, nome)
        return False
    return True


# O que o cliente e o CTN esperam do pagamento: só quando o aviso de esgotado diz que um deles
# faltava é que a nota de concluído faz sentido. O divergente e o em dobro esgotam sem nenhum.
_DA_CONCLUSAO = (Pendencia.BAIXA.value, Pendencia.MENSAGEM.value)


def _concluir(
    sessao: Session, deps: DependenciasConcluir, pagamento: Pagamento, agora: datetime
) -> None:
    """Refaz as pendências do pagamento e reagenda o que falhou.

    O AVISO_CONCLUIDO é uma marca na lista, posta quando o aviso de esgotado sai com a baixa ou a
    mensagem faltando: ele só roda quando nada mais resta.
    """
    concluido = Pendencia.AVISO_CONCLUIDO.value
    aviso = Pendencia.AVISO_ESGOTADO.value
    anteriores = list(pagamento.pendencias)
    marcado = concluido in anteriores
    restantes = [
        nome
        for nome in anteriores
        if nome != concluido and not _passo(sessao, deps, pagamento, nome, agora)
    ]
    if aviso in anteriores and aviso not in restantes:
        marcado = marcado or any(nome in _DA_CONCLUSAO for nome in anteriores)
    if not restantes and marcado and pagamento.atencao_em is not None:
        # O atendente foi chamado pelo teto: avisa que a conclusão saiu. Se falhar, tenta de novo.
        if _passo(sessao, deps, pagamento, concluido, agora):
            marcado = False
        else:
            restantes = [concluido]
    pagamento.pendencias = restantes
    if marcado and restantes and concluido not in restantes:
        pagamento.pendencias = [*restantes, concluido]
    if not restantes:
        pagamento.proxima_tentativa_em = None
        return
    pagamento.tentativas_conclusao += 1
    pagamento.proxima_tentativa_em = agora + deps.politica.espera_integracao(
        pagamento.tentativas_conclusao
    )
    esgotou = pagamento.tentativas_conclusao >= deps.politica.integracao_tentativas
    if esgotou and pagamento.atencao_em is None and aviso not in restantes:
        log.error(
            "pagamento %s pago, mas sem concluir %s depois de %s tentativas: conferir à mão",
            pagamento.id,
            restantes,
            pagamento.tentativas_conclusao,
        )
        if not _passo(sessao, deps, pagamento, aviso, agora):
            pagamento.pendencias = [*restantes, aviso]
        elif any(nome in _DA_CONCLUSAO for nome in restantes):
            pagamento.pendencias = [*restantes, concluido]


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
            .where(
                Pagamento.id == pagamento_id,
                Pagamento.proxima_tentativa_em.is_not(None),
                # Outra instância pode ter tratado e reagendado entre a lista e a trava.
                Pagamento.proxima_tentativa_em <= agora,
            )
            .with_for_update(skip_locked=True)
        )
        if pagamento is None:
            continue
        _concluir(sessao, deps, pagamento, agora)
        sessao.commit()
        tratados += 1
    return tratados


def contar_sem_concluir(sessao: Session) -> int:
    """Os pagamentos pagos cuja conclusão esgotou as tentativas e segue pendente (o /saude)."""
    return sessao.scalar(
        select(func.count(Pagamento.id)).where(
            Pagamento.atencao_em.is_not(None), Pagamento.proxima_tentativa_em.is_not(None)
        )
    )
