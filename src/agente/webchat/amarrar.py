"""Amarra a conversa do webchat à cobrança, para o atendente ter um ticket só (tarefa 7.3).

O cliente entra no widget identificado por `cobranca-<id>` (ver `identidade`). Quando a conversa do
webchat abre, o backend a liga à cobrança: grava o id nos atributos, deixa a nota com o resumo,
etiqueta e atribui ao mesmo time, e marca a conversa do WhatsApp como transferida.
"""

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.config import Config
from agente.db.modelos import (
    AmarracaoWebchat,
    Cobranca,
    Conversa,
    LaudoGravado,
    Mensagem,
    Turno,
)
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import Chatwoot
from agente.politica import Politica
from agente.tipos import OrigemMensagem
from agente.webchat.identidade import cobranca_do_identificador

log = logging.getLogger(__name__)

ETIQUETA_WEBCHAT = "webchat"
ETIQUETA_TRANSFERIDA = "transferida_webchat"


def _link(url_conversas: str | None, conversa_id: int) -> str:
    return f"{url_conversas.rstrip('/')}/{conversa_id}" if url_conversas else f"#{conversa_id}"


def _laudos_do_ultimo_turno(sessao: Session, conversa: Conversa) -> list[LaudoGravado]:
    turno_id = sessao.scalar(
        select(Turno.id)
        .join(LaudoGravado)
        .where(Turno.conversa_id == conversa.id)
        .order_by(Turno.id.desc())
        .limit(1)
    )
    if turno_id is None:
        return []
    return list(
        sessao.scalars(
            select(LaudoGravado).where(LaudoGravado.turno_id == turno_id).order_by(LaudoGravado.id)
        )
    )


def _resumo(
    sessao: Session, cobranca: Cobranca, whatsapp: Conversa | None, url_conversas: str | None
) -> str:
    linhas = [
        "Conversa do webchat, amarrada à cobrança do WhatsApp.",
        f"Cliente: {cobranca.cliente.nome}",
        f"Dívida: {formatar_reais(cobranca.divida_centavos)} "
        f"({cobranca.mensalidades_abertas} mensalidade(s) em aberto, régua {cobranca.regua.value})",
    ]
    if whatsapp is None:
        linhas.append("Conversa do WhatsApp: ainda não ligada ao Chatwoot.")
        return "\n".join(linhas)
    enviadas = sessao.scalars(
        select(Mensagem.id).where(
            Mensagem.conversa_id == whatsapp.id, Mensagem.origem == OrigemMensagem.AGENTE
        )
    ).all()
    forma = whatsapp.forma_enviada.value if whatsapp.forma_enviada else "nenhuma"
    linhas.append(
        f"Propostas: degrau {whatsapp.degrau_atual}; forma de pagamento enviada: {forma}; "
        f"mensagens do agente: {len(enviadas)}"
    )
    if whatsapp.motivo_humano:
        linhas.append(f"Motivo da saída para o humano: {whatsapp.motivo_humano.value}")
    laudos = _laudos_do_ultimo_turno(sessao, whatsapp)
    if laudos:
        linhas.append("Laudo do Jev no último turno:")
        linhas += [
            f"- {laudo.pergunta}: {laudo.valor} (p={laudo.probabilidade:.2f}, "
            f"c={laudo.confianca:.2f})"
            for laudo in laudos
        ]
    linhas.append(f"Conversa do WhatsApp: {_link(url_conversas, whatsapp.chatwoot_conversa_id)}")
    return "\n".join(linhas)


def amarrar_conversa_webchat(
    sessao: Session,
    chatwoot: Chatwoot,
    conversa_webchat_id: int,
    cobranca_id: int,
    *,
    time_id: int | None,
    agora: datetime,
    url_conversas: str | None = None,
) -> None:
    """Liga a conversa do webchat à cobrança. Repetir para a mesma conversa não faz nada.

    A marca fica no banco (`cobranca.webchat_conversa_id`) e só é gravada depois das chamadas ao
    Chatwoot: se uma falhar, a repetição refaz tudo (pode duplicar a nota) em vez de deixar o
    atendente sem o resumo.
    """
    cobranca = sessao.get(Cobranca, cobranca_id)
    if cobranca is None:
        raise ValueError(f"a cobrança {cobranca_id} não existe")
    if cobranca.webchat_conversa_id == conversa_webchat_id:
        return
    whatsapp = sessao.scalar(
        select(Conversa)
        .where(Conversa.cobranca_id == cobranca_id, Conversa.chatwoot_conversa_id.is_not(None))
        .order_by(Conversa.id.desc())
    )

    chatwoot.gravar_atributos(conversa_webchat_id, {"cobranca_id": cobranca_id})
    chatwoot.nota_privada(conversa_webchat_id, _resumo(sessao, cobranca, whatsapp, url_conversas))
    etiquetas = [ETIQUETA_WEBCHAT]
    if whatsapp is not None and whatsapp.motivo_humano is not None:
        etiquetas.append(whatsapp.motivo_humano.value.lower())
    chatwoot.etiquetar(conversa_webchat_id, etiquetas)
    if time_id is not None:
        chatwoot.atribuir(conversa_webchat_id, time_id)
    chatwoot.mudar_status(conversa_webchat_id, "open")
    if whatsapp is not None:
        origem = whatsapp.chatwoot_conversa_id
        chatwoot.etiquetar(origem, [ETIQUETA_TRANSFERIDA])
        chatwoot.gravar_atributos(
            origem, {"transferida_webchat": True, "conversa_webchat_id": conversa_webchat_id}
        )
        chatwoot.nota_privada(
            origem,
            f"Atendimento transferido para o webchat: {_link(url_conversas, conversa_webchat_id)}",
        )
    cobranca.webchat_conversa_id = conversa_webchat_id
    sessao.commit()
    log.info("webchat: conversa %s amarrada à cobrança %s", conversa_webchat_id, cobranca_id)


def _url_das_conversas(config: Config) -> str | None:
    if config.chatwoot_url and config.chatwoot_conta is not None:
        return (
            f"{config.chatwoot_url.rstrip('/')}/app/accounts/{config.chatwoot_conta}/conversations"
        )
    return None


def _guardar_falha(
    sessao: Session,
    pendente: AmarracaoWebchat | None,
    cobranca_id: int,
    conversa_webchat_id: int,
    politica: Politica,
    agora: datetime,
) -> AmarracaoWebchat:
    """Registra uma tentativa que falhou: a próxima espera mais (`espera_integracao`)."""
    if pendente is None:
        pendente = AmarracaoWebchat(
            cobranca_id=cobranca_id, conversa_webchat_id=conversa_webchat_id, tentativas=0
        )
        sessao.add(pendente)
    pendente.tentativas += 1
    pendente.proxima_tentativa_em = agora + politica.espera_integracao(pendente.tentativas)
    if pendente.tentativas >= politica.integracao_tentativas and pendente.atencao_em is None:
        pendente.atencao_em = agora
        log.error(
            "webchat: a amarração da conversa %s (cobrança %s) falhou %s vezes: conferir à mão",
            conversa_webchat_id,
            cobranca_id,
            pendente.tentativas,
        )
    return pendente


def _tentar(
    sessao: Session,
    chatwoot: Chatwoot,
    pendente: AmarracaoWebchat | None,
    cobranca_id: int,
    conversa_webchat_id: int,
    config: Config,
    politica: Politica,
    time_id: int | None,
    agora: datetime,
) -> bool:
    """Amarra; se o Chatwoot falha, deixa a amarração pendente. Devolve se concluiu."""
    try:
        amarrar_conversa_webchat(
            sessao,
            chatwoot,
            conversa_webchat_id,
            cobranca_id,
            time_id=time_id,
            agora=agora,
            url_conversas=_url_das_conversas(config),
        )
    except Exception as erro:
        sessao.rollback()
        if isinstance(erro, httpx.HTTPError):
            log.warning("webchat: amarração da conversa %s falhou (%s)", conversa_webchat_id, erro)
        else:
            log.exception("webchat: amarração da conversa %s falhou", conversa_webchat_id)
        if pendente is not None:
            pendente = sessao.get(AmarracaoWebchat, pendente.id)
        _guardar_falha(sessao, pendente, cobranca_id, conversa_webchat_id, politica, agora)
        sessao.commit()
        return False
    if pendente is not None:
        sessao.delete(sessao.get(AmarracaoWebchat, pendente.id))
        sessao.commit()
    return True


def refazer_amarracoes(
    sessao: Session,
    chatwoot: Chatwoot,
    config: Config,
    politica: Politica,
    time_id: int | None,
    agora: datetime,
) -> int:
    """A rotina do Trabalhador: refaz as amarrações pendentes que já passaram da espera.

    Devolve quantas concluiu. O teto `integracao_tentativas` só marca o aviso (`atencao_em`, que o
    /saude/webchat conta): a rotina segue tentando, com a espera máxima, como a dos pagamentos.
    """
    vencidas = sessao.scalars(
        select(AmarracaoWebchat)
        .where(AmarracaoWebchat.proxima_tentativa_em <= agora)
        .order_by(AmarracaoWebchat.id)
    ).all()
    concluidas = 0
    for pendente in vencidas:
        cobranca = sessao.get(Cobranca, pendente.cobranca_id)
        if cobranca.webchat_conversa_id == pendente.conversa_webchat_id:
            sessao.delete(pendente)  # outro evento já concluiu
            sessao.commit()
            continue
        if _tentar(
            sessao,
            chatwoot,
            pendente,
            pendente.cobranca_id,
            pendente.conversa_webchat_id,
            config,
            politica,
            time_id,
            agora,
        ):
            concluidas += 1
    return concluidas


def contar_amarracoes_sem_concluir(sessao: Session) -> int:
    """As amarrações que esgotaram as tentativas e seguem pendentes (o /saude/webchat)."""
    return sessao.scalar(
        select(func.count(AmarracaoWebchat.id)).where(AmarracaoWebchat.atencao_em.is_not(None))
    )


def da_inbox_do_webchat(dados: dict, config: Config) -> bool:
    inbox = dados.get("inbox_id") or (dados.get("inbox") or {}).get("id")
    if config.webchat_inbox is None or inbox != config.webchat_inbox:
        return False
    conta = dados.get("account_id") or (dados.get("account") or {}).get("id")
    return config.chatwoot_conta is None or conta is None or conta == config.chatwoot_conta


def _leitura(dados: dict) -> tuple[int, object] | None:
    """A conversa e o `identifier` do contato, no `conversation_created` ou no `message_created`.

    No `message_created` só vale a mensagem do cliente (incoming, não privada, de contato): é ela
    que dá nova chance à conversa que nasceu antes de o widget se identificar. O formato dos dois
    eventos é o da documentação do Chatwoot, a confirmar na T3 com o Chatwoot real.
    """
    evento = dados.get("event")
    if evento == "conversation_created":
        sender = (dados.get("meta") or {}).get("sender") or {}
        return int(dados["id"]), sender.get("identifier")
    if evento == "message_created":
        if dados.get("message_type") != "incoming" or dados.get("private"):
            return None
        remetente = dados.get("sender") or {}
        if remetente.get("type", "contact") != "contact":
            return None
        conversa = dados.get("conversation") or {}
        sender = (conversa.get("meta") or {}).get("sender") or {}
        identificador = remetente.get("identifier") or sender.get("identifier")
        return int(conversa["id"]), identificador
    return None


def tratar_evento_webchat(
    sessao: Session,
    chatwoot: Chatwoot,
    dados: dict,
    config: Config,
    politica: Politica,
    time_id: int | None,
    agora: datetime,
) -> str:
    """O `conversation_created` ou o `message_created` da inbox Website.

    Devolve `amarrada`, `pendente` (o Chatwoot falhou: a rotina refaz) ou `ignorado`. É idempotente:
    a conversa já amarrada não faz nada.
    """
    if not da_inbox_do_webchat(dados, config):
        return "ignorado"
    leitura = _leitura(dados)
    if leitura is None:
        return "ignorado"
    conversa_webchat_id, identificador = leitura
    cobranca_id = cobranca_do_identificador(identificador)
    cobranca = sessao.get(Cobranca, cobranca_id) if cobranca_id is not None else None
    if cobranca is None or cobranca.webchat_conversa_id == conversa_webchat_id:
        return "ignorado"
    # O `identifier` (cobranca-<id>) é adivinhável e, sem a validação obrigatória, forjável: só
    # amarra a cobrança que acertou o CPF na página há pouco.
    validade = timedelta(minutes=politica.webchat_verificacao_validade_minutos)
    if cobranca.webchat_verificado_em is None or agora - cobranca.webchat_verificado_em > validade:
        log.warning("webchat: identifier da cobrança %s sem verificação recente", cobranca_id)
        return "ignorado"
    pendente = sessao.scalar(
        select(AmarracaoWebchat).where(AmarracaoWebchat.conversa_webchat_id == conversa_webchat_id)
    )
    if pendente is not None and pendente.proxima_tentativa_em > agora:
        return "pendente"  # a rotina cuida; não bate no Chatwoot a cada mensagem
    concluiu = _tentar(
        sessao,
        chatwoot,
        pendente,
        cobranca_id,
        conversa_webchat_id,
        config,
        politica,
        time_id,
        agora,
    )
    return "amarrada" if concluiu else "pendente"
