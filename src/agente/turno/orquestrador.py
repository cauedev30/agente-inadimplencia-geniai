"""O turno de uma conversa: gates, Jev 1, Jev 2, backend, LLM, Jev 3 e envio (arquitetura).

Em modo sombra, nada é enviado e o estado não muda: o turno só grava os laudos e o que teria feito
(arquitetura, seção 8), mesmo com um humano na conversa (T7). No modo ativo, o turno executa as
ações, redige, valida, envia e muda o estado, ou sai para o humano. O AGENTE_MODO em sombra força a
sombra em tudo; no ativo, a `liberacao` do politica.toml decide cada conversa.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from agente.db.historico import (
    abrir_turno,
    fechar_turno,
    gravar_laudos,
    registrar_mensagem,
    transicionar,
)
from agente.db.modelos import Conversa, Mensagem, Turno
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, CTNFalso
from agente.integracoes.disparador import Disparador
from agente.integracoes.jev import EstadoJev, Jev
from agente.integracoes.pix import PixBanco, PixFalso
from agente.integracoes.redator import Redator, RedatorFalso
from agente.politica import Politica
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Laudo,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    TipoFixa,
)
from agente.turno.acoes import Acao
from agente.turno.avaliacao import Avaliacao, avaliar, estado_jev, numeros_previstos
from agente.turno.catalogo import TEXTO_RESPOSTA_SEGURA
from agente.turno.executar import executar, proximo_estado
from agente.turno.fila import agendar_turno, mensagens_pendentes, tomar_turno
from agente.turno.gates import (
    Adiar,
    Fixa,
    Gate,
    Ignorar,
    Reiniciar,
    Retomar,
    Seguir,
    avaliar_gates,
    humano_na_conversa,
)
from agente.turno.handoff import fazer_handoff
from agente.turno.jev1 import laudo_do_risco
from agente.turno.liberacao import modo_da_conversa
from agente.turno.redacao import Redacao, montar_pedido, redigir_validado

log = logging.getLogger(__name__)


@dataclass
class Dependencias:
    politica: Politica
    chatwoot: Chatwoot
    disparador: Disparador
    time_humano: int | None
    modo: Modo
    jev: Jev
    # Até a T9 e a T1, o CTN e o Pix são os falsos.
    ctn: CTN = field(default_factory=CTNFalso)
    pix: PixBanco = field(default_factory=PixFalso)
    redator: Redator = field(default_factory=RedatorFalso)


def _rotulo(gate: Gate) -> str | None:
    if isinstance(gate, Fixa):
        return f"FIXA:{gate.tipo.value}"
    if isinstance(gate, Retomar):
        return "RETOMADA"
    if isinstance(gate, Reiniciar):
        return "REINICIO"
    return None


def _retomar(sessao: Session, conversa: Conversa, deps: Dependencias, agora: datetime) -> None:
    nome = conversa.cobranca.cliente.nome.split()[0]
    template = deps.politica.template_retomada
    deps.disparador.enviar_template(
        conversa.cobranca.cliente.telefone, template, {"nome": nome}, []
    )
    # Gravada como template, fora do teto: não é a 6ª mensagem.
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.ABERTURA, f"[template {template}]", None, False, agora
    )


def _reiniciar(sessao: Session, conversa: Conversa) -> None:
    """Abre uma conversa nova para a mesma cobrança; a antiga fica no histórico."""
    chatwoot_id = conversa.chatwoot_conversa_id
    conversa.chatwoot_conversa_id = None
    sessao.flush()
    sessao.add(Conversa(cobranca_id=conversa.cobranca_id, chatwoot_conversa_id=chatwoot_id))
    sessao.flush()


def _historico(sessao: Session, conversa_id: int) -> list[tuple[str, str]]:
    mensagens = sessao.scalars(
        select(Mensagem).where(Mensagem.conversa_id == conversa_id).order_by(Mensagem.id)
    )
    return [(mensagem.origem.value, mensagem.texto) for mensagem in mensagens]


def _gravar_laudos(
    sessao: Session, turno_id: int, avaliacao: Avaliacao, origem: str
) -> dict[tuple[BatchJev, str], int]:
    """Grava os laudos do Jev 1 e do Jev 2; devolve o id de cada um por (batch, pergunta)."""
    lotes = []
    if avaliacao.leitura is not None:
        lotes.append((BatchJev.JEV1, list(avaliacao.leitura.laudos)))
    if avaliacao.decisao is not None:
        lotes.append((BatchJev.JEV2, [avaliacao.decisao.laudo]))
    ids = {}
    for batch, laudos in lotes:
        gravados = gravar_laudos(sessao, turno_id, batch, laudos, origem)
        ids |= {(batch, laudo.pergunta): i for laudo, i in zip(laudos, gravados, strict=True)}
    return ids


def _gravar_jev3(
    sessao: Session, turno_id: int, redacao: Redacao, origem: str
) -> list[tuple[Laudo, int]]:
    """Grava os laudos de cada tentativa; devolve os que reprovaram a última, com o id."""
    reprovados = []
    for tentativa in redacao.tentativas:
        gravados = gravar_laudos(sessao, turno_id, BatchJev.JEV3, list(tentativa.laudos), origem)
        reprovados = [
            (laudo, laudo_id)
            for laudo, laudo_id in zip(tentativa.laudos, gravados, strict=True)
            if laudo.pergunta in tentativa.reprovadas
        ]
    return reprovados


def _fechar(
    sessao: Session,
    turno: Turno,
    resultado: ResultadoTurno,
    avaliacao: Avaliacao,
    numeros: dict,
    agora: datetime,
    texto_enviado: str | None = None,
    redacao: dict | None = None,
) -> ResultadoTurno:
    fechar_turno(
        sessao,
        turno.id,
        resultado,
        acao_principal=avaliacao.rotulo,
        acoes_anexadas=[acao.value for acao in avaliacao.anexos],
        numeros=numeros,
        texto_enviado=texto_enviado,
        redacao=redacao,
        momento=agora,
    )
    log.info(
        "turno %s da conversa %s: %s, %s", turno.id, turno.conversa_id, avaliacao.rotulo, resultado
    )
    return resultado


def _handoff(
    sessao: Session,
    conversa: Conversa,
    motivo: MotivoHumano,
    deps: Dependencias,
    agora: datetime,
    laudos: list[Laudo] | None = None,
    laudo_id: int | None = None,
) -> None:
    fazer_handoff(
        sessao,
        deps.chatwoot,
        conversa,
        motivo,
        laudos or [],
        deps.politica,
        deps.time_humano,
        agora,
        laudo_id=laudo_id,
    )


def _enviar(
    sessao: Session, conversa: Conversa, texto: str, deps: Dependencias, agora: datetime
) -> None:
    """Envia a mensagem do agente. Ela conta no teto (D33)."""
    if conversa.chatwoot_conversa_id is None:
        raise ValueError(f"a conversa {conversa.id} não está ligada a uma conversa do Chatwoot")
    chatwoot_id = deps.chatwoot.enviar_mensagem(conversa.chatwoot_conversa_id, texto)
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.AGENTE, texto, chatwoot_id, True, agora)


def _ja_saiu_resposta_segura(sessao: Session, conversa_id: int) -> bool:
    consulta = select(
        exists().where(
            Mensagem.conversa_id == conversa_id,
            Mensagem.origem == OrigemMensagem.AGENTE,
            Mensagem.texto == TEXTO_RESPOSTA_SEGURA,
        )
    )
    return bool(sessao.scalar(consulta))


def _laudo_da_transicao(avaliacao: Avaliacao, ids: dict[tuple[BatchJev, str], int]) -> int:
    """O risco aponta o laudo do Jev 1 que o causou; o resto, a decisão do Jev 2 (auditoria)."""
    if avaliacao.em_risco:
        return ids[(BatchJev.JEV1, laudo_do_risco(avaliacao.leitura).pergunta)]
    return ids[(BatchJev.JEV2, "acao")]


def _mudar_estado(
    sessao: Session, conversa: Conversa, para: EstadoConversa, laudo_id: int, agora: datetime
) -> None:
    if para is not conversa.estado:
        transicionar(sessao, conversa, para, None, laudo_id, momento=agora)


def _agir(
    sessao: Session,
    conversa: Conversa,
    turno: Turno,
    avaliacao: Avaliacao,
    estado: EstadoJev,
    historico: list[tuple[str, str]],
    deps: Dependencias,
    ids: dict[tuple[BatchJev, str], int],
    agora: datetime,
) -> ResultadoTurno:
    """O backend executa, o LLM redige, o Jev 3 valida e o texto sai (tarefa 5.3)."""
    cobranca = conversa.cobranca
    # O que o backend grava só vale se o texto com os números sair.
    ponto = sessao.begin_nested()
    numeros = executar(
        [avaliacao.acao, *avaliacao.anexos],
        cobranca,
        conversa,
        deps,
        agora,
        objecao=avaliacao.leitura.sim("objecao_valor"),
    )
    destino = proximo_estado(conversa.estado, numeros.forma, avaliacao.em_risco)
    pedido = montar_pedido(
        numeros,
        avaliacao.leitura,
        cobranca.regua,
        cobranca.cliente.nome,
        destino,
        historico,
        deps.politica,
    )
    redacao = redigir_validado(deps.redator, deps.jev, pedido, numeros, estado)
    if redacao.texto is None:
        # O degrau, a objeção, a forma e o Pix pendente voltam atrás.
        ponto.rollback()
    else:
        ponto.commit()
    reprovados = _gravar_jev3(sessao, turno.id, redacao, deps.jev.origem)

    texto = redacao.texto
    if texto is not None:
        _enviar(sessao, conversa, texto, deps, agora)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        saida, resultado = "LLM", ResultadoTurno.ENVIADO
    elif redacao.motivo is not None:
        _handoff(sessao, conversa, redacao.motivo, deps, agora)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    elif not _ja_saiu_resposta_segura(sessao, conversa.id):
        # Reprovou duas vezes: a resposta segura do catálogo, uma vez por conversa (seção 6).
        texto = TEXTO_RESPOSTA_SEGURA
        _enviar(sessao, conversa, texto, deps, agora)
        saida, resultado = "RESPOSTA_SEGURA", ResultadoTurno.ENVIADO
    else:
        laudos = [laudo for laudo, _ in reprovados]
        laudo_id = reprovados[0][1] if reprovados else None
        _handoff(sessao, conversa, MotivoHumano.VALIDACAO, deps, agora, laudos, laudo_id)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    return _fechar(
        sessao,
        turno,
        resultado,
        avaliacao,
        numeros.como_dict(),
        agora,
        texto_enviado=texto,
        redacao=redacao.como_dict(saida),
    )


def _seguir(
    sessao: Session, conversa: Conversa, turno: Turno, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    cobranca = conversa.cobranca
    historico = _historico(sessao, conversa.id)
    estado = estado_jev(conversa, cobranca, historico, deps.politica)
    avaliacao = avaliar(deps.jev, estado, conversa, cobranca, deps.politica)
    ids = _gravar_laudos(sessao, turno.id, avaliacao, deps.jev.origem)
    if deps.modo is Modo.SOMBRA:
        previstos = numeros_previstos(avaliacao, conversa, cobranca, deps.politica)
        return _fechar(sessao, turno, ResultadoTurno.SOMBRA, avaliacao, previstos, agora)
    if avaliacao.motivo is not None:
        laudos, laudo_id = [], None
        if avaliacao.causa is not None:
            batch, laudo = avaliacao.causa
            laudos, laudo_id = [laudo], ids[(batch, laudo.pergunta)]
        _handoff(sessao, conversa, avaliacao.motivo, deps, agora, laudos, laudo_id)
        erro = {"erro": avaliacao.erro} if avaliacao.erro else {}
        return _fechar(sessao, turno, ResultadoTurno.HANDOFF, avaliacao, erro, agora)
    if avaliacao.acao is Acao.NONE:
        # Não agir: nada é redigido nem enviado.
        destino = proximo_estado(conversa.estado, None, avaliacao.em_risco)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        return _fechar(sessao, turno, ResultadoTurno.SEM_ENVIO, avaliacao, {}, agora)
    return _agir(sessao, conversa, turno, avaliacao, estado, historico, deps, ids, agora)


def processar_turno(
    sessao: Session, conversa_id: int, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    conversa = sessao.get_one(Conversa, conversa_id)
    if deps.modo is Modo.ATIVO:
        # O modo ativo vale só para a conversa liberada no politica.toml (tarefa 5.5).
        deps = replace(deps, modo=modo_da_conversa(conversa.cobranca, deps.politica))
    gate = avaliar_gates(
        conversa,
        mensagens_pendentes(sessao, conversa_id),
        agora,
        deps.politica,
        sessao,
        barrar_humano=deps.modo is Modo.ATIVO,
    )
    if isinstance(gate, Adiar):
        # As mensagens ficam pendentes e o turno volta para a fila na abertura (D16).
        agendar_turno(sessao, conversa_id, gate.ate)
        return ResultadoTurno.ADIADO

    mensagens = tomar_turno(sessao, conversa_id)
    turno = abrir_turno(sessao, conversa_id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)

    def fechar(resultado: ResultadoTurno) -> ResultadoTurno:
        fechar_turno(sessao, turno.id, resultado, acao_principal=_rotulo(gate), momento=agora)
        return resultado

    if isinstance(gate, Ignorar):
        return fechar(ResultadoTurno.IGNORADO)
    if isinstance(gate, Seguir):
        return _seguir(sessao, conversa, turno, deps, agora)
    if deps.modo is Modo.SOMBRA:
        return fechar(ResultadoTurno.SOMBRA)
    if isinstance(gate, Fixa) and gate.tipo is TipoFixa.CANAL_EXTERNO:
        _handoff(sessao, conversa, MotivoHumano.TETO, deps, agora)
        return fechar(ResultadoTurno.FIXA)
    if isinstance(gate, Retomar):
        _retomar(sessao, conversa, deps, agora)
        return fechar(ResultadoTurno.FIXA)
    if isinstance(gate, Reiniciar):
        _reiniciar(sessao, conversa)
        return fechar(ResultadoTurno.IGNORADO)
    # A confirmação de pagamento não sai dos gates do turno (tarefa 6.1).
    return fechar(ResultadoTurno.SOMBRA)


def criar_processador(deps: Dependencias) -> Callable[[Session, int, datetime], None]:
    """Adapta o turno ao laço de trabalho da fila."""

    def processar(sessao: Session, conversa_id: int, agora: datetime) -> None:
        processar_turno(sessao, conversa_id, agora, deps)

    return processar
