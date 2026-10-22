"""O turno de uma conversa: gates, Jev 1, Jev 2, backend, LLM, Jev 3 e envio (arquitetura).

Em modo sombra, nada é enviado e o estado não muda: o turno só grava os laudos e o que teria feito
(arquitetura, seção 8), mesmo com um humano na conversa (T7). No modo ativo, o turno executa as
ações, redige, valida, grava a mensagem, muda o estado e só então a envia (o envio é o último
efeito e é confirmado depois), ou sai para o humano. O AGENTE_MODO em sombra força a sombra em
tudo; no ativo, a `liberacao` do politica.toml decide cada conversa.
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
from agente.dominio.janela import dentro_da_janela, proxima_abertura
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
    EstadoEnvio,
    Laudo,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
    TipoFixa,
)
from agente.turno.acoes import Acao
from agente.turno.avaliacao import Avaliacao, avaliar, estado_jev, numeros_previstos
from agente.turno.catalogo import TEXTO_RESPOSTA_SEGURA
from agente.turno.executar import contar_objecao, executar, proximo_estado
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
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa_id, Mensagem.envio != EstadoEnvio.DESCARTADA)
        .order_by(Mensagem.id)
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
    rotulo: str | None = None,
) -> ResultadoTurno:
    rotulo = rotulo or avaliacao.rotulo
    fechar_turno(
        sessao,
        turno.id,
        resultado,
        acao_principal=rotulo,
        acoes_anexadas=[acao.value for acao in avaliacao.anexos],
        numeros=numeros,
        texto_enviado=texto_enviado,
        redacao=redacao,
        momento=agora,
    )
    log.info("turno %s da conversa %s: %s, %s", turno.id, turno.conversa_id, rotulo, resultado)
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


def _gravar_envio(sessao: Session, conversa: Conversa, texto: str, agora: datetime) -> None:
    """Grava a intenção de enviar a mensagem do agente. Ela já conta no teto (D33).

    O Chatwoot só a recebe depois, em `_despachar`, quando o turno já está gravado.
    """
    if conversa.chatwoot_conversa_id is None:
        raise ValueError(f"a conversa {conversa.id} não está ligada a uma conversa do Chatwoot")
    registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.AGENTE,
        texto,
        None,
        True,
        agora,
        envio=EstadoEnvio.PENDENTE,
    )


def _envio_pendente(sessao: Session, conversa_id: int) -> Mensagem | None:
    consulta = select(Mensagem).where(
        Mensagem.conversa_id == conversa_id,
        Mensagem.origem == OrigemMensagem.AGENTE,
        Mensagem.envio == EstadoEnvio.PENDENTE,
    )
    return sessao.scalars(consulta.order_by(Mensagem.id)).first()


def _entregue(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, deps: Dependencias
) -> str | None:
    """O id da mensagem no Chatwoot, se ela já foi entregue (a resposta do envio se perdeu).

    Só vale a de mesmo texto que o banco ainda não conhece: o mesmo texto já saiu em outro turno.
    """
    conhecidas = set(
        sessao.scalars(
            select(Mensagem.chatwoot_message_id).where(Mensagem.chatwoot_message_id.is_not(None))
        )
    )
    for chatwoot_id, texto in deps.chatwoot.mensagens_do_agente(conversa.chatwoot_conversa_id):
        if chatwoot_id not in conhecidas and texto.strip() == mensagem.texto.strip():
            return chatwoot_id
    return None


def _confirmar(sessao: Session, mensagem: Mensagem, chatwoot_id: str) -> None:
    mensagem.chatwoot_message_id = chatwoot_id
    mensagem.envio = EstadoEnvio.ENVIADA
    sessao.flush()


def _descartar(sessao: Session, mensagem: Mensagem) -> None:
    """A mensagem nunca saiu e não vai mais sair: deixa de contar no teto."""
    mensagem.envio = EstadoEnvio.DESCARTADA
    mensagem.conta_no_teto = False
    sessao.flush()


def _despachar(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, deps: Dependencias, *, reenvio: bool
) -> None:
    """Manda ao Chatwoot a mensagem gravada: o último efeito externo do turno.

    No reenvio, a tentativa anterior pode ter entregue e perdido a resposta: confere antes.
    """
    chatwoot_id = _entregue(sessao, conversa, mensagem, deps) if reenvio else None
    if chatwoot_id is None:
        chatwoot_id = deps.chatwoot.enviar_mensagem(conversa.chatwoot_conversa_id, mensagem.texto)
    _confirmar(sessao, mensagem, chatwoot_id)


def _ja_saiu_resposta_segura(sessao: Session, conversa_id: int) -> bool:
    consulta = select(
        exists().where(
            Mensagem.conversa_id == conversa_id,
            Mensagem.origem == OrigemMensagem.AGENTE,
            Mensagem.texto == TEXTO_RESPOSTA_SEGURA,
            Mensagem.envio != EstadoEnvio.DESCARTADA,
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
    """O backend executa, o LLM redige, o Jev 3 valida e o texto fica gravado para sair (5.3)."""
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
        _gravar_envio(sessao, conversa, texto, agora)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        saida, resultado = "LLM", ResultadoTurno.ENVIADO
    elif redacao.motivo is not None:
        _handoff(sessao, conversa, redacao.motivo, deps, agora)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    elif not _ja_saiu_resposta_segura(sessao, conversa.id):
        # Reprovou duas vezes: a resposta segura do catálogo, uma vez por conversa (seção 6).
        texto = TEXTO_RESPOSTA_SEGURA
        _gravar_envio(sessao, conversa, texto, agora)
        saida, resultado = "RESPOSTA_SEGURA", ResultadoTurno.ENVIADO
    else:
        laudos = [laudo for laudo, _ in reprovados]
        laudo_id = reprovados[0][1] if reprovados else None
        _handoff(sessao, conversa, MotivoHumano.VALIDACAO, deps, agora, laudos, laudo_id)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    if redacao.texto is None:
        # O savepoint desfez a ação: o turno não registra degrau, link nem Pix que não valem.
        numeros_gravados, rotulo = {"executado": False}, avaliacao.rotulo_nao_executado
    else:
        numeros_gravados, rotulo = numeros.como_dict(), None
    return _fechar(
        sessao,
        turno,
        resultado,
        avaliacao,
        numeros_gravados,
        agora,
        texto_enviado=texto,
        redacao=redacao.como_dict(saida),
        rotulo=rotulo,
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
        # Não agir: nada é redigido nem enviado, mas a objeção respondida com NONE também conta.
        if avaliacao.leitura.sim("objecao_valor"):
            contar_objecao(conversa, cobranca, deps.politica)
        destino = proximo_estado(conversa.estado, None, avaliacao.em_risco)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        return _fechar(sessao, turno, ResultadoTurno.SEM_ENVIO, avaliacao, {}, agora)
    return _agir(sessao, conversa, turno, avaliacao, estado, historico, deps, ids, agora)


def _desistir(
    sessao: Session, conversa: Conversa, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Esgotadas as tentativas com a integração fora, a conversa sai para o humano, uma vez só."""
    if not dentro_da_janela(agora, deps.politica):
        # O handoff manda a mensagem do canal externo: só dentro da janela (D16).
        agendar_turno(sessao, conversa.id, proxima_abertura(agora, deps.politica))
        return ResultadoTurno.ADIADO
    pendente = _envio_pendente(sessao, conversa.id)
    if pendente is not None:
        # Que não se perca o que chegou a sair: confere o Chatwoot antes de descartar.
        chatwoot_id = _entregue(sessao, conversa, pendente, deps)
        if chatwoot_id is None:
            _descartar(sessao, pendente)
        else:
            _confirmar(sessao, pendente, chatwoot_id)
    mensagens = tomar_turno(sessao, conversa.id)
    turno = abrir_turno(sessao, conversa.id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    if not humano_na_conversa(conversa) and conversa.cobranca.status is StatusCobranca.ATIVA:
        _handoff(sessao, conversa, MotivoHumano.INTEGRACAO_INDISPONIVEL, deps, agora)
    conversa.falhas_turno = 0
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.HANDOFF,
        redacao={"saida": "HANDOFF", "erro": MotivoHumano.INTEGRACAO_INDISPONIVEL.value},
        momento=agora,
    )
    return ResultadoTurno.HANDOFF


def _registrar_falha(
    sessao: Session,
    conversa_id: int,
    erro: Exception,
    deps: Dependencias,
    agora: datetime,
    *,
    desistindo: bool,
) -> ResultadoTurno:
    """Conta a tentativa que falhou, agenda a próxima com espera maior ou passa ao humano.

    Chega aqui com o turno já desfeito. Só o tipo do erro é gravado: a mensagem pode trazer URL ou
    matrícula.
    """
    conversa = sessao.get_one(Conversa, conversa_id)
    conversa.falhas_turno += 1
    mensagens = mensagens_pendentes(sessao, conversa_id)
    turno = abrir_turno(sessao, conversa_id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.ERRO,
        redacao={"erro": type(erro).__name__, "tentativa": conversa.falhas_turno},
        momento=agora,
    )
    if not desistindo and conversa.falhas_turno >= deps.politica.integracao_tentativas:
        try:
            with sessao.begin_nested():
                return _desistir(sessao, conversa, deps, agora)
        except Exception:
            log.exception("o handoff da conversa %s também falhou", conversa_id)
    conversa.turno_em = agora + deps.politica.espera_integracao(conversa.falhas_turno)
    sessao.flush()
    return ResultadoTurno.ERRO


def _limpar_retorno(
    sessao: Session, conversa: Conversa, agora: datetime, retorno: datetime | None
) -> None:
    """Tira da fila o retorno que já cumpriu o papel; o que o cliente agendou depois fica.

    Se o cliente escreveu na espera, o turno dele roda logo em seguida.
    """
    if conversa.turno_em is None:
        return
    if conversa.turno_em == retorno or conversa.turno_em <= agora:
        conversa.turno_em = agora if mensagens_pendentes(sessao, conversa.id) else None


def _entregar(
    sessao: Session,
    conversa: Conversa,
    mensagem: Mensagem,
    deps: Dependencias,
    agora: datetime,
    *,
    reenvio: bool,
    retorno: datetime | None,
) -> ResultadoTurno:
    """Despacha a mensagem gravada. A falha conta como uma tentativa e fica para a próxima."""
    try:
        _despachar(sessao, conversa, mensagem, deps, reenvio=reenvio)
    except Exception as erro:
        log.exception("o envio da conversa %s falhou", conversa.id)
        return _registrar_falha(sessao, conversa.id, erro, deps, agora, desistindo=False)
    conversa.falhas_turno = 0
    _limpar_retorno(sessao, conversa, agora, retorno)
    sessao.flush()
    return ResultadoTurno.ENVIADO


def _retomar_envio(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Um turno anterior gravou a mensagem e não a confirmou: refaz só o envio, não o turno."""
    if humano_na_conversa(conversa) or conversa.cobranca.status is not StatusCobranca.ATIVA:
        # O humano assumiu ou a cobrança fechou na espera: o texto não sai mais.
        _descartar(sessao, mensagem)
        _limpar_retorno(sessao, conversa, agora, None)
        return ResultadoTurno.IGNORADO
    if not dentro_da_janela(agora, deps.politica):
        agendar_turno(sessao, conversa.id, proxima_abertura(agora, deps.politica))
        return ResultadoTurno.ADIADO
    return _entregar(sessao, conversa, mensagem, deps, agora, reenvio=True, retorno=None)


def _turno_ativo(
    sessao: Session, conversa: Conversa, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    """O turno ativo com as integrações no ar: se uma falha, o turno inteiro é desfeito.

    A conversa conta as falhas seguidas e espera mais a cada uma. Depois de
    `integracao_tentativas`, o turno nem roda: sai um handoff só (arquitetura, seção 9).
    O envio ao Chatwoot é o último passo: o turno é gravado e confirmado antes, com a mensagem
    PENDENTE, e o retorno agendado cobre o processo que cair no meio.
    """
    desistindo = conversa.falhas_turno >= deps.politica.integracao_tentativas
    pendente = _envio_pendente(sessao, conversa.id)
    if pendente is not None and not desistindo:
        return _retomar_envio(sessao, conversa, pendente, deps, agora)
    try:
        with sessao.begin_nested():
            if desistindo:
                resultado = _desistir(sessao, conversa, deps, agora)
            else:
                resultado = _rodar_turno(sessao, conversa, agora, deps)
    except Exception as erro:
        log.exception("o turno da conversa %s falhou", conversa.id)
        return _registrar_falha(sessao, conversa.id, erro, deps, agora, desistindo=desistindo)
    pendente = _envio_pendente(sessao, conversa.id)
    if pendente is None:
        if conversa.falhas_turno and resultado is not ResultadoTurno.ADIADO:
            conversa.falhas_turno = 0
            sessao.flush()
        return resultado
    retorno = agora + deps.politica.espera_integracao(1)
    conversa.turno_em = retorno
    sessao.commit()
    return _entregar(sessao, conversa, pendente, deps, agora, reenvio=False, retorno=retorno)


def _rodar_turno(
    sessao: Session, conversa: Conversa, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    conversa_id = conversa.id
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


def processar_turno(
    sessao: Session, conversa_id: int, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    conversa = sessao.get_one(Conversa, conversa_id)
    if deps.modo is Modo.ATIVO:
        # O modo ativo vale só para a conversa liberada no politica.toml (tarefa 5.5).
        deps = replace(deps, modo=modo_da_conversa(conversa.cobranca, deps.politica))
    if deps.modo is Modo.ATIVO:
        return _turno_ativo(sessao, conversa, agora, deps)
    return _rodar_turno(sessao, conversa, agora, deps)


def criar_processador(deps: Dependencias) -> Callable[[Session, int, datetime], None]:
    """Adapta o turno ao laço de trabalho da fila."""

    def processar(sessao: Session, conversa_id: int, agora: datetime) -> None:
        processar_turno(sessao, conversa_id, agora, deps)

    return processar
