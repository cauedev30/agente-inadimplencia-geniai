"""O turno de uma conversa: os gates, o Jev 1, o Jev 2 e as regras de ação. O LLM entra na etapa 5.

Em modo sombra, nada é enviado e o estado não muda: o turno só grava os laudos e o que teria feito
(arquitetura, seção 8). No modo ativo, a saída para o humano já acontece; o envio é da tarefa 5.3.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.historico import (
    abrir_turno,
    fechar_turno,
    gravar_laudos,
    registrar_mensagem,
)
from agente.db.modelos import Conversa, Mensagem, Turno
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.disparador import Disparador
from agente.integracoes.jev import Jev
from agente.politica import Politica
from agente.tipos import BatchJev, Modo, MotivoHumano, OrigemMensagem, ResultadoTurno, TipoFixa
from agente.turno.avaliacao import Avaliacao, avaliar, estado_jev, numeros_previstos
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
)
from agente.turno.handoff import fazer_handoff

log = logging.getLogger(__name__)


@dataclass
class Dependencias:
    politica: Politica
    chatwoot: Chatwoot
    disparador: Disparador
    time_humano: int | None
    modo: Modo
    jev: Jev


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


def _gravar_laudos(sessao: Session, turno_id: int, avaliacao: Avaliacao, origem: str) -> dict:
    """Grava os laudos do Jev 1 e do Jev 2 e devolve o id de cada um, para a auditoria."""
    ids: dict = {}
    if avaliacao.leitura is not None:
        laudos = list(avaliacao.leitura.laudos)
        ids |= zip(
            laudos, gravar_laudos(sessao, turno_id, BatchJev.JEV1, laudos, origem), strict=True
        )
    if avaliacao.decisao is not None:
        [laudo_id] = gravar_laudos(
            sessao, turno_id, BatchJev.JEV2, [avaliacao.decisao.laudo], origem
        )
        ids[avaliacao.decisao.laudo] = laudo_id
    return ids


def _seguir(
    sessao: Session, conversa: Conversa, turno: Turno, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    cobranca = conversa.cobranca
    estado = estado_jev(conversa, cobranca, _historico(sessao, conversa.id), deps.politica)
    avaliacao = avaliar(deps.jev, estado, conversa, cobranca, deps.politica)
    ids = _gravar_laudos(sessao, turno.id, avaliacao, deps.jev.origem)
    resultado = ResultadoTurno.SOMBRA
    if avaliacao.motivo is not None and deps.modo is Modo.ATIVO:
        causa = avaliacao.laudo_da_causa
        fazer_handoff(
            sessao,
            deps.chatwoot,
            conversa,
            avaliacao.motivo,
            [causa] if causa is not None else [],
            deps.politica,
            deps.time_humano,
            agora,
            laudo_id=ids.get(causa),
        )
        resultado = ResultadoTurno.HANDOFF
    fechar_turno(
        sessao,
        turno.id,
        resultado,
        acao_principal=avaliacao.rotulo,
        acoes_anexadas=[acao.value for acao in avaliacao.anexos],
        numeros=numeros_previstos(avaliacao, conversa, cobranca, deps.politica),
        momento=agora,
    )
    log.info("turno %s da conversa %s: %s", turno.id, conversa.id, avaliacao.rotulo)
    return resultado


def processar_turno(
    sessao: Session, conversa_id: int, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    conversa = sessao.get_one(Conversa, conversa_id)
    gate = avaliar_gates(
        conversa, mensagens_pendentes(sessao, conversa_id), agora, deps.politica, sessao
    )
    if isinstance(gate, Adiar):
        # As mensagens ficam pendentes e o turno volta para a fila na abertura (D16).
        agendar_turno(sessao, conversa_id, gate.ate)
        return ResultadoTurno.ADIADO

    mensagens = tomar_turno(sessao, conversa_id)
    turno = abrir_turno(sessao, conversa_id, [m.id for m in mensagens], deps.modo)

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
        fazer_handoff(
            sessao,
            deps.chatwoot,
            conversa,
            MotivoHumano.TETO,
            [],
            deps.politica,
            deps.time_humano,
            agora,
        )
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
