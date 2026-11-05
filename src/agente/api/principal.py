"""Monta o serviço de produção a partir das variáveis de ambiente.

Rodar: `uvicorn agente.api.principal:montar_app --factory`. Sem o Chatwoot configurado (T3), o
serviço só roda em modo sombra, com a interface falsa. O modo ativo exige o Chatwoot real, o Jev
real e, enquanto o Pix e o CTN forem os falsos, a liberação em piloto. Sem o AGENTE_JEV_API_KEY, o
Jev é o falso; com ela, o serviço só sobe com o AGENTE_JEV_DADOS_REAIS_LIBERADOS=true (a ZDR da
TypeSafe, D12). Sem o AGENTE_LLM_API_KEY, o redator é o falso; sem o AGENTE_TRANSCRICAO_*, o áudio
não é transcrito. Com AGENTE_TURNO=uma_chamada (D47), o turno é uma chamada só ao Luna: ela exige o
AGENTE_LLM_API_KEY, e o modo ativo pede essa chave no lugar da do Jev. O Jev falso só existe na
montagem: o turno e o botão do Pix não o consultam nesse caminho.
"""

import logging

from fastapi import FastAPI

from agente.api.app import criar_app
from agente.config import MENSAGEM_JEV_SEM_LIBERACAO, Config, carregar_config
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import Jev, JevComPrazo, JevFalso
from agente.integracoes.jev_typesafe import JevTypeSafe
from agente.integracoes.llm_turno import LLMTurno, LLMTurnoOpenAI
from agente.integracoes.redator import Redator, RedatorFalso, RedatorOpenAI
from agente.integracoes.transcritor import Transcritor, TranscritorOpenAI
from agente.logs import configurar_logging
from agente.politica import LiberacaoPiloto, Politica, carregar_politica
from agente.tipos import Modo
from agente.turno.orquestrador import Dependencias, criar_processador
from agente.webchat.link import ConfigWebchat

log = logging.getLogger(__name__)


def _redator(config: Config) -> Redator:
    if config.llm_api_key:
        return RedatorOpenAI(
            config.llm_api_key,
            config.llm_modelo,
            url=config.llm_url,
            corpo_extra=config.llm_corpo_extra,
        )
    log.warning("AGENTE_LLM_API_KEY não configurada: usando o redator falso (T6)")
    return RedatorFalso()


def _jev(config: Config, politica: Politica) -> Jev:
    if config.jev_api_key:
        if not config.jev_dados_reais_liberados:
            raise ValueError(MENSAGEM_JEV_SEM_LIBERACAO)
        return JevComPrazo(
            JevTypeSafe.da_config(config, politica.jev_timeout_ms), politica.jev_timeout_ms
        )
    log.warning("AGENTE_JEV_API_KEY não configurada: usando o Jev falso (T10)")
    return JevComPrazo(JevFalso(), politica.jev_timeout_ms)


def _llm_turno(config: Config) -> LLMTurno | None:
    """O LLM do turno de uma chamada (D47); None no pipeline, que é o padrão."""
    if config.turno == "pipeline":
        return None
    if not config.llm_api_key:
        raise ValueError("AGENTE_TURNO=uma_chamada exige o AGENTE_LLM_API_KEY (D47)")
    log.info("turno de uma chamada (D47)")
    return LLMTurnoOpenAI(
        config.llm_api_key,
        config.llm_modelo,
        url=config.llm_url,
        corpo_extra=config.llm_corpo_extra,
    )


def _transcritor(config: Config, politica: Politica) -> Transcritor | None:
    if config.transcricao_ligada:
        return TranscritorOpenAI(
            config.transcricao_api_key,
            config.transcricao_modelo_usado,
            url=config.transcricao_url,
            timeout=politica.transcricao_prazo_segundos,
            max_caracteres=politica.transcricao_max_caracteres,
        )
    log.warning(
        "transcrição do áudio desligada (AGENTE_TRANSCRICAO_*): o áudio cai no pedido de texto"
    )
    return None


def _webchat(config: Config) -> ConfigWebchat | None:
    """O link do webchat só sai se a página estiver ligada: senão o cliente cai num 503."""
    if not config.webchat_url_base:
        return None
    if not config.webchat_ligado:
        log.warning(
            "AGENTE_WEBCHAT_URL_BASE sem o resto da configuração do webchat (segredo, chave da "
            "identidade, token do site e AGENTE_CHATWOOT_URL): a 6ª mensagem leva o link provisório"
        )
        return None
    return ConfigWebchat(config.webchat_url_base, config.webchat_segredo)


def _exigir_o_modo_ativo_seguro(config: Config, politica: Politica, modo: Modo) -> None:
    """O modo ativo só sobe com o avaliador real do caminho e, enquanto o Pix e o CTN forem os
    falsos, no piloto.

    O avaliador é o Jev real no pipeline e o Luna no turno de uma chamada (D47, decisão 10).
    """
    if modo is not Modo.ATIVO:
        return
    # O robô nunca age sem avaliação (arquitetura, seção 9).
    if config.turno == "uma_chamada":
        if not config.llm_api_key:
            raise ValueError(
                "o modo ativo com AGENTE_TURNO=uma_chamada exige o AGENTE_LLM_API_KEY (D47)"
            )
    elif not config.jev_api_key:
        raise ValueError("o modo ativo exige o Jev real (AGENTE_JEV_API_KEY, T10)")
    # Um Pix falso nunca pode chegar a um cliente de verdade: só os telefones da lista saem.
    if not isinstance(politica.liberacao, LiberacaoPiloto):
        raise ValueError(
            "enquanto o Pix e o CTN forem os falsos (T1, T9), o modo ativo só roda com a "
            'liberação em piloto (liberacao = { modo = "piloto", telefones = [...] } no '
            "politica.toml)"
        )


def montar_dependencias(config: Config) -> Dependencias:
    modo = Modo.ATIVO if config.modo == "ativo" else Modo.SOMBRA
    if config.chatwoot_url and config.chatwoot_conta and config.chatwoot_token:
        chatwoot = ChatwootHttp(config.chatwoot_url, config.chatwoot_conta, config.chatwoot_token)
    elif modo is Modo.ATIVO:
        raise ValueError("o modo ativo exige o Chatwoot configurado (T3)")
    else:
        log.warning("Chatwoot não configurado: usando a interface falsa, em modo sombra")
        chatwoot = ChatwootFalso()
    politica = carregar_politica(config.politica_path)
    try:
        _exigir_o_modo_ativo_seguro(config, politica, modo)
        jev = _jev(config, politica)
        llm_turno = _llm_turno(config)
    except ValueError:
        if isinstance(chatwoot, ChatwootHttp):
            chatwoot.fechar()
        raise
    return Dependencias(
        politica=politica,
        chatwoot=chatwoot,
        # Até a T4, o disparador é falso: o template de retomada não sai de verdade.
        disparador=DisparadorFalso(),
        time_humano=config.chatwoot_time_humano,
        modo=modo,
        jev=jev,
        redator=_redator(config),
        transcritor=_transcritor(config, politica),
        webchat=_webchat(config),
        llm_turno=llm_turno,
        # Até a T9, o CTN é o falso e não tem a carteira: no ativo (só no piloto, acima) ele aceita
        # a matrícula, senão todo turno com link do CTN falha com ClienteNaoEncontrado.
        ctn=CTNFalso(aceita_qualquer_matricula=modo is Modo.ATIVO),
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
