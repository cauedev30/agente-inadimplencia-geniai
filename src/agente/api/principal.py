"""Monta o serviço de produção a partir das variáveis de ambiente.

Rodar: `uvicorn agente.api.principal:montar_app --factory`. Sem o Chatwoot configurado (T3), o
serviço só roda em modo sombra, com a interface falsa. Até a T10, o Jev também é o falso. Sem o
AGENTE_GROQ_API_KEY, o redator é o falso.
"""

import logging

from fastapi import FastAPI

from agente.api.app import criar_app
from agente.config import Config, carregar_config
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevComPrazo, JevFalso
from agente.integracoes.redator import Redator, RedatorFalso, RedatorGroq
from agente.logs import configurar_logging
from agente.politica import carregar_politica
from agente.tipos import Modo
from agente.turno.orquestrador import Dependencias, criar_processador
from agente.webchat.link import ConfigWebchat

log = logging.getLogger(__name__)


def _redator(config: Config) -> Redator:
    if config.groq_api_key:
        return RedatorGroq(config.groq_api_key, config.groq_modelo)
    log.warning("AGENTE_GROQ_API_KEY não configurada: usando o redator falso (T6)")
    return RedatorFalso()


def _webchat(config: Config) -> ConfigWebchat | None:
    if config.webchat_url_base and config.webchat_segredo:
        return ConfigWebchat(config.webchat_url_base, config.webchat_segredo)
    if config.webchat_url_base:
        log.warning("AGENTE_WEBCHAT_SEGREDO ausente: a 6ª mensagem leva o link provisório")
    return None


def montar_dependencias(config: Config) -> Dependencias:
    modo = Modo.ATIVO if config.modo == "ativo" else Modo.SOMBRA
    if config.chatwoot_url and config.chatwoot_conta and config.chatwoot_token:
        chatwoot = ChatwootHttp(config.chatwoot_url, config.chatwoot_conta, config.chatwoot_token)
    elif modo is Modo.ATIVO:
        raise ValueError("o modo ativo exige o Chatwoot configurado (T3)")
    else:
        log.warning("Chatwoot não configurado: usando a interface falsa, em modo sombra")
        chatwoot = ChatwootFalso()
    if modo is Modo.ATIVO:
        # O robô nunca age sem avaliação (arquitetura, seção 9), e o Jev ainda é o falso.
        chatwoot.fechar()
        raise ValueError("o modo ativo exige o Jev real (T10)")
    politica = carregar_politica(config.politica_path)
    # Até a T10, não há Jev real: os laudos gravados são os da falsa, sem valor de calibração.
    log.warning("Jev real ainda não ligado (T10): usando a interface falsa")
    return Dependencias(
        politica=politica,
        chatwoot=chatwoot,
        # Até a T4, o disparador é falso: o template de retomada não sai de verdade.
        disparador=DisparadorFalso(),
        time_humano=config.chatwoot_time_humano,
        modo=modo,
        jev=JevComPrazo(JevFalso(), politica.jev_timeout_ms),
        redator=_redator(config),
        webchat=_webchat(config),
    )


def montar_app(config: Config | None = None, *, iniciar_trabalhador: bool = True) -> FastAPI:
    config = config or carregar_config()
    configurar_logging(config.log_level)
    deps = montar_dependencias(config)
    return criar_app(
        criar_sessao(config.database_url),
        deps.politica,
        config,
        processar=criar_processador(deps),
        dependencias=deps,
        ctn=deps.ctn,
        iniciar_trabalhador=iniciar_trabalhador,
    )
