"""Monta o serviço de produção a partir das variáveis de ambiente.

Rodar: `uvicorn agente.api.principal:montar_app --factory`. O serviço exige o Chatwoot real (T3);
sem ele não sobe. Só os telefones do piloto conversam (D61). O turno é uma chamada só ao Luna
(D47, D59): o serviço só sobe com o AGENTE_LLM_API_KEY. Com o AGENTE_CTN_API_KEY, o CTN lê da EFICAZ
(a escrita segue falsa). Com qualquer dado real (CTN real ou Chatwoot real) e qualquer destino na
OpenAI (o Luna ou a transcrição), o serviço só sobe com o AGENTE_LLM_DADOS_REAIS_LIBERADOS=true
(M2). Sem o AGENTE_TRANSCRICAO_*, o áudio não é transcrito.
"""

import logging
from datetime import datetime

from fastapi import FastAPI

from agente.api.app import criar_app
from agente.config import Config, carregar_config, mensagem_llm_sem_liberacao
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootHttp
from agente.integracoes.ctn import CTN, CTNComposto, CTNFalso
from agente.integracoes.ctn_eficaz import CTNEficaz
from agente.integracoes.ctn_teste import CTNDeCasos, CTNEscritaDeTeste, RoteadorCTN
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurno, LLMTurnoOpenAI
from agente.integracoes.pix import PixDeTeste, PixFalso
from agente.integracoes.transcritor import Transcritor, TranscritorOpenAI
from agente.logs import configurar_logging
from agente.politica import Politica, carregar_politica
from agente.teste.casos import exigir_piloto_fora_da_carteira
from agente.turno.orquestrador import Dependencias, criar_processador

log = logging.getLogger(__name__)


def _exigir_a_openai_liberada_com_dado_real(config: Config) -> None:
    """Com dado real de cliente (CTN real ou Chatwoot real), o texto e o áudio só vão à OpenAI
    (o Luna e a transcrição) depois da conferência da retenção (M2).

    A mensagem do erro não leva nenhum valor.
    """
    fontes = config.fontes_de_dado_real
    destinos = config.destinos_externos_do_cliente
    if fontes and destinos and not config.llm_dados_reais_liberados:
        raise ValueError(mensagem_llm_sem_liberacao(fontes, destinos))


def _ctn(config: Config, politica: Politica) -> tuple[CTN, CTNDeCasos | None]:
    """O CTN do serviço e, no modo teste, o `CTNDeCasos` que o testador comanda (None sem ele).

    Sem `[teste]`: o CTN real só de leitura (EFICAZ) com a escrita falsa; sem a chave, o falso de
    hoje. Com `[teste]` (D56): o roteador manda as matrículas `SIM-` aos casos do cardápio e o
    resto ao CTN real (a EFICAZ, ou um falso vazio no ensaio sem a chave); o link e a baixa são os
    `[TESTE]`.
    """
    teste = politica.teste
    if teste is not None:
        real: CTN = CTNEficaz.da_config(config) if config.ctn_real_ligado else CTNFalso()
        zona = politica.janela.zona
        casos = CTNDeCasos(
            teste.casos, politica.mensalidade_centavos, lambda: datetime.now(zona).date()
        )
        return CTNComposto(RoteadorCTN(casos, real), CTNEscritaDeTeste()), casos
    # Até a T9, a escrita (link, cadastro do cartão e baixa) é a falsa: ela aceita a matrícula,
    # senão todo turno com link do CTN falha com ClienteNaoEncontrado.
    falso = CTNFalso(aceita_qualquer_matricula=True)
    if config.ctn_real_ligado:
        return CTNComposto(CTNEficaz.da_config(config), falso), None
    return falso, None


def _llm_turno(config: Config) -> LLMTurno:
    """O Luna do turno (D47, D59). O robô nunca age sem ele (arquitetura, seção 9)."""
    if not config.llm_api_key:
        raise ValueError("o serviço exige o AGENTE_LLM_API_KEY (D59)")
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


def montar_dependencias(config: Config) -> Dependencias:
    if not config.chatwoot_real_ligado:
        raise ValueError("o serviço exige o Chatwoot configurado (T3)")
    chatwoot = ChatwootHttp(config.chatwoot_url, config.chatwoot_conta, config.chatwoot_token)
    politica = carregar_politica(config.politica_path)
    try:
        _exigir_a_openai_liberada_com_dado_real(config)
        llm_turno = _llm_turno(config)
    except ValueError:
        chatwoot.fechar()
        raise
    ctn, casos = _ctn(config, politica)
    return Dependencias(
        politica=politica,
        chatwoot=chatwoot,
        # Até a T4, o disparador é falso: o template de abertura não sai de verdade.
        disparador=DisparadorFalso(),
        time_humano=config.chatwoot_time_humano,
        transcritor=_transcritor(config, politica),
        llm_turno=llm_turno,
        ctn=ctn,
        # Só o CTN real tem os lançamentos para conferir; o falso daria "sem meses" a todos. No modo
        # teste os casos também têm (o `CTNDeCasos`), e a conferência é o que os grava.
        conferir_no_contato=config.ctn_real_ligado or casos is not None,
        pix=PixDeTeste() if casos is not None else PixFalso(),
        casos=casos,
    )


def _fechar(deps: Dependencias) -> None:
    """Fecha os clientes HTTP de `deps` quando a subida é recusada depois de montá-las."""
    for cliente in (deps.chatwoot, deps.ctn):
        fechar = getattr(cliente, "fechar", None)
        if callable(fechar):
            fechar()


def montar_app(config: Config | None = None, *, iniciar_trabalhador: bool = True) -> FastAPI:
    config = config or carregar_config()
    configurar_logging(config.log_level)
    deps = montar_dependencias(config)
    fabrica = criar_sessao(config.database_url)
    if deps.politica.teste is not None:
        # A trava de subida do modo teste só roda com ele: sem `[teste]` o banco nem é consultado.
        try:
            with fabrica() as sessao:
                exigir_piloto_fora_da_carteira(sessao, deps.politica)
        except ValueError:
            _fechar(deps)
            raise
    return criar_app(
        fabrica,
        deps.politica,
        config,
        processar=criar_processador(deps),
        dependencias=deps,
        ctn=deps.ctn,
        iniciar_trabalhador=iniciar_trabalhador,
    )
