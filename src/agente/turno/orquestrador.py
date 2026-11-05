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

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from agente.db.historico import (
    abrir_turno,
    fechar_turno,
    gravar_laudos,
    mensagens_no_teto,
    registrar_mensagem,
    transicionar,
)
from agente.db.modelos import Conversa, Mensagem, Turno
from agente.dominio.janela import dentro_da_janela, proxima_abertura
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNFalso, CTNIndisponivel
from agente.integracoes.disparador import Disparador, FalhaNoEnvio
from agente.integracoes.jev import EstadoJev, Jev, JevIndisponivel
from agente.integracoes.llm_turno import LLMTurno
from agente.integracoes.pix import PixBanco, PixFalso, PixIndisponivel
from agente.integracoes.redator import Redator, RedatorFalso, RedatorIndisponivel
from agente.integracoes.transcritor import Transcritor
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
from agente.turno.avaliacao import (
    NAO_EXECUTADA,
    Avaliacao,
    avaliar,
    estado_jev,
    numeros_previstos,
    rotulo_humano,
)
from agente.turno.catalogo import TEXTO_RESPOSTA_SEGURA, DadosFixa, mensagem_fixa
from agente.turno.entrega import confirmar_envio, entregue
from agente.turno.executar import contar_objecao, executar, proximo_estado
from agente.turno.fila import agendar_turno, mensagens_pendentes, tomar_turno
from agente.turno.gates import (
    Adiar,
    Fixa,
    Gate,
    Ignorar,
    PagarPix,
    Reiniciar,
    Retomar,
    Seguir,
    avaliar_envio,
    avaliar_gates,
    e_comando_de_reinicio,
    humano_na_conversa,
)
from agente.turno.handoff import fazer_handoff, ja_desconfiou, ja_saiu_pelo_teto
from agente.turno.jev1 import LeituraJev1, desconfia_no_limiar, laudo_do_risco
from agente.turno.liberacao import modo_da_conversa
from agente.turno.redacao import (
    Redacao,
    RedacaoInvalida,
    anexar_recebedor,
    interpolar,
    montar_pedido,
    redigir_validado,
)
from agente.turno.transcricao import (
    LONGO_DEMAIS,
    audios_sem_resultado,
    pedido_de_texto,
    texto_da_mensagem,
    transcrever_audios,
)
from agente.turno.uma_chamada import (
    ORIGEM_UMA_CHAMADA,
    ResolucaoTurno,
    montar_entrada,
    palavra_chave,
    previa_numeros,
    resolver_turno,
)
from agente.webchat.link import ConfigWebchat

log = logging.getLogger(__name__)

# O que o turno grava como ação principal quando pede o texto do áudio que não foi lido (D43).
ROTULO_PEDIR_TEXTO = f"FIXA:{TipoFixa.PEDIR_TEXTO.value}"
# O que o turno de uma chamada grava como ação principal quando o cliente não é o devedor (D47).
ROTULO_TERCEIRO = "TERCEIRO"

# O que conta como integração fora do ar (CTN, Pix, Chatwoot, redator e Jev): é tentado de novo e
# depois vai ao humano. Qualquer outro erro é bug de código: tem o próprio teto (`erros_turno`).
_FALHAS_DE_INTEGRACAO = (
    httpx.HTTPError,
    ClienteNaoEncontrado,
    CTNIndisponivel,
    FalhaNoEnvio,
    JevIndisponivel,
    PixIndisponivel,
    RedatorIndisponivel,
)


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
    # O áudio do cliente vira texto antes do Jev (D43). None: a transcrição está desligada (sem a
    # chave) e o áudio cai no pedido de texto.
    transcritor: Transcritor | None = None
    # O domínio e o segredo do link do webchat (G7); sem eles, a 6ª mensagem leva o link provisório.
    webchat: ConfigWebchat | None = None
    # Com ele, o turno é o de uma chamada ao Luna (D47) e o Jev não é usado; sem ele, o pipeline.
    llm_turno: LLMTurno | None = None


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


def _reiniciar(
    sessao: Session,
    conversa: Conversa,
    turno: Turno,
    mensagens: list[Mensagem],
    deps: Dependencias,
    agora: datetime,
) -> None:
    """Abre uma conversa nova para a mesma cobrança; a antiga fica no histórico.

    A confirmação sai antes de a conversa trocar, fora do teto, e fica gravada na antiga, para o
    histórico da nova começar limpo. As mensagens da rajada que vieram depois do comando passam para
    a nova, com um turno agendado (sem isso, o `/reset` e a mensagem seguinte, dentro dos mesmos
    7 s, deixavam a 2ª sem resposta); as anteriores ao comando continuam descartadas.
    """
    chatwoot_id = conversa.chatwoot_conversa_id
    texto = deps.politica.texto_reinicio
    # Se o Chatwoot falhar, o turno inteiro é desfeito e tentado de novo (`_turno_ativo`).
    confirmacao = deps.chatwoot.enviar_mensagem(chatwoot_id, texto)
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.FIXA, texto, confirmacao, False, agora)
    posicao = max(i for i, m in enumerate(mensagens) if e_comando_de_reinicio(m, deps.politica))
    conversa.chatwoot_conversa_id = None
    sessao.flush()
    nova = Conversa(cobranca_id=conversa.cobranca_id, chatwoot_conversa_id=chatwoot_id)
    sessao.add(nova)
    sessao.flush()
    depois = mensagens[posicao + 1 :]
    for mensagem in depois:
        mensagem.conversa_id = nova.id
    # O turno do reinício fica só com o que ele consumiu; o resto é do turno da conversa nova.
    turno.mensagens_ids = [m.id for m in mensagens[: posicao + 1]]
    if depois:
        agendar_turno(sessao, nova.id, agora)
    sessao.flush()


def _historico(sessao: Session, conversa_id: int) -> list[tuple[str, str]]:
    mensagens = sessao.scalars(
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa_id, Mensagem.envio != EstadoEnvio.DESCARTADA)
        .order_by(Mensagem.id)
    )
    # O áudio do cliente entra como o texto transcrito, com o rótulo (D43).
    return [(mensagem.origem.value, texto_da_mensagem(mensagem)) for mensagem in mensagens]


def _historico_e_novas(
    sessao: Session, conversa_id: int, ids_do_turno: list[int]
) -> tuple[list[tuple[str, str]], list[str]]:
    """O histórico de `_historico`, com as mensagens deste turno (a rajada) separadas como novas."""
    mensagens = sessao.scalars(
        select(Mensagem)
        .where(Mensagem.conversa_id == conversa_id, Mensagem.envio != EstadoEnvio.DESCARTADA)
        .order_by(Mensagem.id)
    )
    historico, novas = [], []
    for mensagem in mensagens:
        texto = texto_da_mensagem(mensagem)
        if mensagem.id in ids_do_turno:
            novas.append(texto)
        else:
            historico.append((mensagem.origem.value, texto))
    return historico, novas


def _ouvir_os_audios(
    sessao: Session, conversa: Conversa, agora: datetime, deps: Dependencias
) -> None:
    """Transcreve os áudios da rajada se o turno vai seguir (D43), antes de qualquer decisão.

    Os gates rodam antes: cobrança inativa, humano na conversa, fora da janela e fora das 24h não
    transcrevem nada (o áudio adiado é transcrito quando o turno roda). Em sombra também transcreve,
    porque o Jev da sombra precisa ler o texto. Sem transcritor, o áudio fica como não ouvido.
    """
    if deps.transcritor is None:
        return
    pendentes = mensagens_pendentes(sessao, conversa.id)
    if not audios_sem_resultado(pendentes):
        return
    gate = avaliar_gates(
        conversa,
        pendentes,
        agora,
        deps.politica,
        sessao,
        barrar_humano=deps.modo is Modo.ATIVO,
    )
    if isinstance(gate, Seguir):
        transcrever_audios(
            sessao,
            conversa,
            pendentes,
            deps.chatwoot,
            deps.transcritor,
            deps.politica,
            com_nota=deps.modo is Modo.ATIVO or deps.politica.nota_transcricao_em_sombra,
        )


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
    if avaliacao.erro:
        # O erro do Jev é texto, não número: fica na redação, que já guarda os erros do turno.
        redacao = {**(redacao or {}), "erro": avaliacao.erro}
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


def _fechar_ignorado(
    sessao: Session,
    turno: Turno,
    avaliacao: Avaliacao,
    agora: datetime,
    *,
    nao_executado: bool = False,
    rotulo: str | None = None,
) -> ResultadoTurno:
    """A cobrança fechou durante o turno (o pagamento chegou): nada do turno vale nem sai.

    O turno de uma chamada passa o `rotulo`, porque a avaliação dele pode não ter ação.
    """
    return _fechar(
        sessao,
        turno,
        ResultadoTurno.IGNORADO,
        avaliacao,
        {"executado": False} if nao_executado else {},
        agora,
        redacao={"saida": "IGNORADO", "motivo": "sem_cobranca_ativa"},
        rotulo=rotulo or (avaliacao.rotulo_nao_executado if nao_executado else None),
    )


def _handoff(
    sessao: Session,
    conversa: Conversa,
    motivo: MotivoHumano,
    deps: Dependencias,
    agora: datetime,
    laudos: list[Laudo] | None = None,
    laudo_id: int | None = None,
    *,
    leitura: LeituraJev1 | None = None,
    enviar_mensagem: bool = True,
    texto: str | None = None,
) -> None:
    """A saída para o humano. A mensagem sai sem link (D44) se o motivo é da lista, se a conversa já
    saiu por um motivo da lista, ou se o Jev 1 deste turno deu `desconfia_golpe` sim, seja qual
    for o motivo que venceu: quem desconfia de golpe não recebe link. O `texto` (D47) troca o texto
    fixo da mensagem ao cliente."""
    sem_link = (
        motivo in deps.politica.motivos_humano_no_whatsapp
        or ja_desconfiou(sessao, conversa, deps.politica)
        or (leitura is not None and desconfia_no_limiar(leitura, deps.politica))
    )
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
        webchat=deps.webchat,
        sem_link=sem_link,
        enviar_mensagem=enviar_mensagem,
        texto=texto,
    )


def _gravar_envio(
    sessao: Session, conversa: Conversa, texto: str, agora: datetime, *, conta_no_teto: bool = True
) -> None:
    """Grava a intenção de enviar a mensagem do agente. Ela já conta no teto (D33), menos o pedido
    de texto do áudio, que sai fora dele (D43).

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
        conta_no_teto,
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
    """O id da mensagem no Chatwoot, se ela já foi entregue (a resposta do envio se perdeu)."""
    return entregue(sessao, conversa, mensagem, deps.chatwoot)


def _descartar(sessao: Session, mensagem: Mensagem) -> None:
    """A mensagem nunca saiu e não vai mais sair: deixa de contar no teto.

    O turno que a gerou fechou como ENVIADO (ou FIXA, o pedido de texto), antes do envio: passa a
    dizer que o texto não saiu.
    """
    mensagem.envio = EstadoEnvio.DESCARTADA
    mensagem.conta_no_teto = False
    turno = sessao.scalars(
        select(Turno)
        .where(
            Turno.conversa_id == mensagem.conversa_id,
            # O pedido de texto do áudio fecha como FIXA, com o texto enviado.
            Turno.resultado.in_((ResultadoTurno.ENVIADO, ResultadoTurno.FIXA)),
            Turno.texto_enviado == mensagem.texto,
        )
        .order_by(Turno.id.desc())
    ).first()
    if turno is not None:
        turno.resultado = ResultadoTurno.DESCARTADO
        turno.texto_enviado = None
        turno.redacao = {**turno.redacao, "envio": EstadoEnvio.DESCARTADA.value}
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
    confirmar_envio(sessao, mensagem, chatwoot_id)


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


def _laudo_da_transicao(avaliacao: Avaliacao, ids: dict[tuple[BatchJev, str], int]) -> int | None:
    """O risco aponta o laudo do Jev 1 que o causou; o resto, a decisão do Jev 2 (auditoria).

    O toque no botão não passa pelo Jev: a transição não tem laudo.
    """
    if avaliacao.leitura is None:
        return None
    if avaliacao.em_risco:
        return ids[(BatchJev.JEV1, laudo_do_risco(avaliacao.leitura).pergunta)]
    return ids[(BatchJev.JEV2, "acao")]


def _mudar_estado(
    sessao: Session, conversa: Conversa, para: EstadoConversa, laudo_id: int | None, agora: datetime
) -> None:
    if para is not conversa.estado:
        transicionar(sessao, conversa, para, None, laudo_id, momento=agora)


def _cobranca_fechou_no_meio(sessao: Session, conversa: Conversa) -> bool:
    """Relê a cobrança (com trava) e a conversa antes de o turno gravar o estado final.

    O Jev e o LLM demoram, e o webhook de pagamento roda em outra transação: se ele confirmou nesse
    meio tempo, a conversa que o turno tem na sessão está velha. Sem a releitura, o turno
    sobrescreveria o ENCERRADO e mandaria o canal externo depois do "pagamento confirmado". A trava
    da conversa e da cobrança segura a confirmação seguinte até o commit do turno.
    """
    sessao.flush()
    # A ordem das travas é a da confirmação do pagamento: conversa, depois cobrança.
    sessao.refresh(conversa, with_for_update={"key_share": True})
    sessao.refresh(conversa.cobranca, with_for_update={"key_share": True})
    return conversa.cobranca.status is not StatusCobranca.ATIVA


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
    # A trava vem antes do savepoint: o rollback dele (texto vazio) soltaria a trava tomada dentro.
    if _cobranca_fechou_no_meio(sessao, conversa):
        return _fechar_ignorado(sessao, turno, avaliacao, agora)
    # O que o backend grava só vale se o texto com os números sair.
    ponto = sessao.begin_nested()
    numeros = executar(
        [avaliacao.acao, *avaliacao.anexos],
        cobranca,
        conversa,
        deps,
        agora,
        objecao=avaliacao.leitura is not None and avaliacao.leitura.sim("objecao_valor"),
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
    fechou = _cobranca_fechou_no_meio(sessao, conversa)
    if redacao.texto is None or fechou:
        # O degrau, a objeção, a forma e o Pix pendente voltam atrás.
        ponto.rollback()
    else:
        ponto.commit()
    reprovados = _gravar_jev3(sessao, turno.id, redacao, deps.jev.origem)
    if fechou:
        return _fechar_ignorado(sessao, turno, avaliacao, agora, nao_executado=True)

    texto = redacao.texto
    if texto is not None:
        # Depois do Jev 3 e antes de gravar: o histórico guarda o texto que sai, com o recebedor
        # (D44).
        texto = anexar_recebedor(texto, numeros, deps.politica)
        _gravar_envio(sessao, conversa, texto, agora)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        saida, resultado = "LLM", ResultadoTurno.ENVIADO
    elif redacao.motivo is not None:
        _handoff(sessao, conversa, redacao.motivo, deps, agora, leitura=avaliacao.leitura)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    elif not _ja_saiu_resposta_segura(sessao, conversa.id):
        # Reprovou duas vezes: a resposta segura do catálogo, uma vez por conversa (seção 6).
        texto = TEXTO_RESPOSTA_SEGURA
        _gravar_envio(sessao, conversa, texto, agora)
        saida, resultado = "RESPOSTA_SEGURA", ResultadoTurno.ENVIADO
    else:
        laudos = [laudo for laudo, _ in reprovados]
        laudo_id = reprovados[0][1] if reprovados else None
        _handoff(
            sessao,
            conversa,
            MotivoHumano.VALIDACAO,
            deps,
            agora,
            laudos,
            laudo_id,
            leitura=avaliacao.leitura,
        )
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
    if deps.llm_turno is not None:
        return _seguir_uma_chamada(sessao, conversa, turno, deps, agora)
    cobranca = conversa.cobranca
    historico = _historico(sessao, conversa.id)
    estado = estado_jev(conversa, cobranca, historico, deps.politica)
    avaliacao = avaliar(deps.jev, estado, conversa, cobranca, deps.politica)
    ids = _gravar_laudos(sessao, turno.id, avaliacao, deps.jev.origem)
    if deps.modo is Modo.SOMBRA:
        previstos = numeros_previstos(avaliacao, conversa, cobranca, deps.politica)
        return _fechar(sessao, turno, ResultadoTurno.SOMBRA, avaliacao, previstos, agora)
    if _cobranca_fechou_no_meio(sessao, conversa):
        return _fechar_ignorado(sessao, turno, avaliacao, agora)
    if avaliacao.motivo is not None:
        laudos, laudo_id = [], None
        if avaliacao.causa is not None:
            batch, laudo = avaliacao.causa
            laudos, laudo_id = [laudo], ids[(batch, laudo.pergunta)]
        _handoff(
            sessao,
            conversa,
            avaliacao.motivo,
            deps,
            agora,
            laudos,
            laudo_id,
            leitura=avaliacao.leitura,
        )
        return _fechar(sessao, turno, ResultadoTurno.HANDOFF, avaliacao, {}, agora)
    if avaliacao.acao is Acao.NONE:
        # Não agir: nada é redigido nem enviado, mas a objeção respondida com NONE também conta.
        if avaliacao.leitura.sim("objecao_valor"):
            contar_objecao(conversa, cobranca, deps.politica)
        destino = proximo_estado(conversa.estado, None, avaliacao.em_risco)
        _mudar_estado(sessao, conversa, destino, _laudo_da_transicao(avaliacao, ids), agora)
        return _fechar(sessao, turno, ResultadoTurno.SEM_ENVIO, avaliacao, {}, agora)
    return _agir(sessao, conversa, turno, avaliacao, estado, historico, deps, ids, agora)


def _avaliacao_uma_chamada(resolucao: ResolucaoTurno) -> Avaliacao:
    """A avaliação do turno novo, para o `_fechar` e o `numeros_previstos`: sem leitura nem decisão.

    A ação só entra quando o turno passou com ela. O terceiro e o que não passou ficam sem ação, e o
    rótulo vai explícito (`_rotulo_uma_chamada`).
    """
    if resolucao.motivo is not None:
        return Avaliacao(None, None, None, [], resolucao.motivo, resolucao.erro)
    veredito = resolucao.veredito
    if _passou(resolucao) and not veredito.terceiro:
        return Avaliacao(None, None, veredito.acao, list(veredito.anexos), None, resolucao.erro)
    return Avaliacao(None, None, None, [], None, resolucao.erro)


def _passou(resolucao: ResolucaoTurno) -> bool:
    """O turno tem o que fazer: um texto aprovado, ou a ação que não envia nada (`NONE`)."""
    veredito = resolucao.veredito
    return veredito is not None and (resolucao.reply is not None or veredito.acao is Acao.NONE)


def _rotulo_uma_chamada(resolucao: ResolucaoTurno) -> str:
    """A ação principal que o turno grava: o humano, o terceiro, a ação, ou a que não saiu."""
    if resolucao.motivo is not None:
        return rotulo_humano(resolucao.motivo)
    if not _passou(resolucao):
        ultima = next((t.acao for t in reversed(resolucao.tentativas) if t.acao), "SEM_ACAO")
        return f"{NAO_EXECUTADA}{ultima}"
    if resolucao.veredito.terceiro:
        return ROTULO_TERCEIRO
    return resolucao.veredito.acao.value


def _gravar_laudos_uma_chamada(
    sessao: Session,
    turno_id: int,
    resolucao: ResolucaoTurno,
    guarda: tuple[MotivoHumano, str] | None,
    golpe_pela_guarda: bool,
) -> tuple[dict[tuple[BatchJev, str], int], list[Laudo]]:
    """Os sinais e o tom em JEV1 (mais a palavra-chave que bateu) e a ação em JEV2 (decisão 8).

    Devolve o id de cada laudo por (batch, pergunta) e os laudos do JEV1, para achar a causa.
    """
    saida = resolucao.saida
    jev1 = list(saida.leitura(golpe_pela_guarda=golpe_pela_guarda).laudos) if saida else []
    if guarda is not None:
        jev1.append(Laudo("palavra_chave", guarda[1], 1.0, 1.0))
    lotes = [(BatchJev.JEV1, jev1)]
    if saida is not None:
        lotes.append((BatchJev.JEV2, [saida.laudo_da_acao()]))
    ids = {}
    for batch, laudos in lotes:
        if laudos:
            gravados = gravar_laudos(sessao, turno_id, batch, laudos, ORIGEM_UMA_CHAMADA)
            ids |= {(batch, laudo.pergunta): i for laudo, i in zip(laudos, gravados, strict=True)}
    return ids, jev1


def _causa_uma_chamada(
    resolucao: ResolucaoTurno, jev1: list[Laudo], ids: dict[tuple[BatchJev, str], int]
) -> tuple[list[Laudo], int | None]:
    """O laudo do JEV1 que levou ao humano (a palavra-chave, o sinal ou o tom), para a nota e a
    transição. Sem veredito (o LLM fora e sem palavra-chave), não há causa."""
    if resolucao.veredito is None:
        return [], None
    tipo, _, nome = resolucao.veredito.causa.partition(":")
    pergunta = {"palavra_chave": "palavra_chave", "tom": "tom"}.get(tipo, nome)
    laudo = next((laudo for laudo in jev1 if laudo.pergunta == pergunta), None)
    if laudo is None:
        return [], None
    return [laudo], ids[(BatchJev.JEV1, pergunta)]


def _nada_passou(
    sessao: Session,
    conversa: Conversa,
    turno: Turno,
    avaliacao: Avaliacao,
    resolucao: ResolucaoTurno,
    rotulo: str,
    leitura: LeituraJev1 | None,
    deps: Dependencias,
    agora: datetime,
    interpolacao: list[str] | None = None,
) -> ResultadoTurno:
    """Nenhum texto passou: a resposta segura, uma vez por conversa, e depois o humano (VALIDACAO).

    O backend não executou nada, ou desfez o que executou: o turno grava `{"executado": False}`.
    """
    if not _ja_saiu_resposta_segura(sessao, conversa.id):
        texto = TEXTO_RESPOSTA_SEGURA
        _gravar_envio(sessao, conversa, texto, agora)
        saida, resultado = "RESPOSTA_SEGURA", ResultadoTurno.ENVIADO
    else:
        texto = None
        _handoff(sessao, conversa, MotivoHumano.VALIDACAO, deps, agora, leitura=leitura)
        saida, resultado = "HANDOFF", ResultadoTurno.HANDOFF
    redacao = resolucao.como_dict(saida)
    if interpolacao:
        redacao["interpolacao_reprovada"] = interpolacao
    return _fechar(
        sessao,
        turno,
        resultado,
        avaliacao,
        {"executado": False},
        agora,
        texto_enviado=texto,
        redacao=redacao,
        rotulo=rotulo,
    )


def _seguir_uma_chamada(
    sessao: Session, conversa: Conversa, turno: Turno, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """O turno de uma chamada ao Luna (D47): o LLM lê, decide e redige; o backend executa e envia.

    A ordem é a do pipeline onde importa: os laudos, a sombra, a releitura da cobrança, o humano, o
    `NONE`, e o savepoint da ação, que só vale se o texto aprovado sair. O texto já foi aprovado
    numa prévia (`resolver_turno`), então a ação só roda depois dele. O estado nunca vai a
    `EM_RISCO`.
    """
    cobranca = conversa.cobranca
    historico, novas = _historico_e_novas(sessao, conversa.id, turno.mensagens_ids)
    guarda = palavra_chave(novas)
    entrada = montar_entrada(
        conversa,
        cobranca,
        deps.politica,
        historico,
        novas,
        mensagens_no_teto(sessao, conversa.id),
        agora,
    )
    resolucao = resolver_turno(
        deps.llm_turno,
        entrada,
        guarda,
        lambda acoes: previa_numeros(acoes, conversa, cobranca, deps.politica, agora),
        cobranca,
    )
    # O golpe pela palavra-chave vale como `desconfia_golpe` sim: nada de link (D44).
    golpe_pela_guarda = guarda is not None and guarda[0] is MotivoHumano.DESCONFIANCA
    ids, jev1 = _gravar_laudos_uma_chamada(sessao, turno.id, resolucao, guarda, golpe_pela_guarda)
    avaliacao = _avaliacao_uma_chamada(resolucao)
    rotulo = _rotulo_uma_chamada(resolucao)
    leitura = (
        resolucao.saida.leitura(golpe_pela_guarda=golpe_pela_guarda) if resolucao.saida else None
    )
    if deps.modo is Modo.SOMBRA:
        previstos = numeros_previstos(avaliacao, conversa, cobranca, deps.politica)
        return _fechar(
            sessao,
            turno,
            ResultadoTurno.SOMBRA,
            avaliacao,
            previstos,
            agora,
            redacao=resolucao.como_dict("SOMBRA"),
            rotulo=rotulo,
        )
    if _cobranca_fechou_no_meio(sessao, conversa):
        return _fechar_ignorado(sessao, turno, avaliacao, agora, rotulo=rotulo)
    if resolucao.motivo is not None:
        laudos, laudo_id = _causa_uma_chamada(resolucao, jev1, ids)
        _handoff(
            sessao,
            conversa,
            resolucao.motivo,
            deps,
            agora,
            laudos,
            laudo_id,
            leitura=leitura,
            texto=resolucao.texto_handoff,
        )
        return _fechar(
            sessao,
            turno,
            ResultadoTurno.HANDOFF,
            avaliacao,
            {},
            agora,
            redacao=resolucao.como_dict("HANDOFF"),
            rotulo=rotulo,
        )
    if not _passou(resolucao):
        return _nada_passou(
            sessao, conversa, turno, avaliacao, resolucao, rotulo, leitura, deps, agora
        )
    veredito = resolucao.veredito
    laudo_da_acao = ids.get((BatchJev.JEV2, "acao"))
    if veredito.acao is Acao.NONE:
        # Não agir (ou o terceiro sem texto): nada é enviado.
        destino = proximo_estado(conversa.estado, None, False)
        _mudar_estado(sessao, conversa, destino, laudo_da_acao, agora)
        return _fechar(
            sessao,
            turno,
            ResultadoTurno.SEM_ENVIO,
            avaliacao,
            {},
            agora,
            redacao=resolucao.como_dict("SEM_ENVIO"),
            rotulo=rotulo,
        )
    # A trava da cobrança já foi tomada acima, antes do savepoint (o rollback dele a soltaria). A
    # releitura depois do `executar` confere se o pagamento chegou nesse meio tempo.
    ponto = sessao.begin_nested()
    acoes = [] if veredito.terceiro else [veredito.acao, *veredito.anexos]
    numeros = executar(acoes, cobranca, conversa, deps, agora, objecao=False)
    interpolacao: list[str] | None = None
    try:
        final = interpolar(resolucao.reply, numeros)
    except RedacaoInvalida as erro:
        # A prévia já aprovou este texto: não deveria acontecer. Nada do que a ação fez vale.
        final, interpolacao = None, erro.motivos
    fechou = _cobranca_fechou_no_meio(sessao, conversa)
    if final is None or fechou:
        ponto.rollback()
    else:
        ponto.commit()
    nao_executada = f"{NAO_EXECUTADA}{rotulo}"
    if fechou:
        return _fechar_ignorado(
            sessao, turno, avaliacao, agora, nao_executado=True, rotulo=nao_executada
        )
    if final is None:
        return _nada_passou(
            sessao,
            conversa,
            turno,
            avaliacao,
            resolucao,
            nao_executada,
            leitura,
            deps,
            agora,
            interpolacao,
        )
    # Depois da aprovação e antes de gravar: o histórico guarda o texto que sai, com o recebedor
    # (D44).
    texto = anexar_recebedor(final, numeros, deps.politica)
    _gravar_envio(sessao, conversa, texto, agora)
    destino = proximo_estado(conversa.estado, numeros.forma, False)
    _mudar_estado(sessao, conversa, destino, laudo_da_acao, agora)
    return _fechar(
        sessao,
        turno,
        ResultadoTurno.ENVIADO,
        avaliacao,
        numeros.como_dict(),
        agora,
        texto_enviado=texto,
        redacao=resolucao.como_dict("LLM"),
        rotulo=rotulo,
    )


def _ja_pediu_texto(sessao: Session, conversa_id: int, modo: Modo) -> bool:
    """Já houve, nesta conversa e neste modo, um turno que pediu (ou pediria) o texto do áudio.

    O modo conta: o pedido que só a sombra gravou nunca chegou ao cliente, então o ativo não o
    toma por a 1ª vez; a sombra, por sua vez, prevê a 2ª vez pelos pedidos da própria sombra.
    O pedido descartado (o texto não saiu) também não conta.
    """
    consulta = select(
        exists().where(
            Turno.conversa_id == conversa_id,
            Turno.modo == modo,
            Turno.acao_principal == ROTULO_PEDIR_TEXTO,
            Turno.resultado.in_((ResultadoTurno.FIXA, ResultadoTurno.SOMBRA)),
        )
    )
    return bool(sessao.scalar(consulta))


def _pedir_texto(
    sessao: Session,
    conversa: Conversa,
    turno: Turno,
    pedido: str,
    deps: Dependencias,
    agora: datetime,
) -> ResultadoTurno:
    """A rajada ficou sem nada legível (só áudio que não foi lido): pede o texto (D43).

    Sem Jev e sem LLM. Na 1ª vez da conversa sai a fixa PEDIR_TEXTO, fora do teto; na 2ª, o humano
    assume pelo caminho de sempre (D5), com o motivo AUDIO. Em sombra, só grava o que faria.
    """
    if deps.modo is Modo.ATIVO and _cobranca_fechou_no_meio(sessao, conversa):
        # O pagamento foi confirmado depois dos gates: nem o pedido nem o handoff valem mais.
        fechar_turno(
            sessao,
            turno.id,
            ResultadoTurno.IGNORADO,
            acao_principal=ROTULO_PEDIR_TEXTO,
            redacao={"saida": "IGNORADO", "motivo": "sem_cobranca_ativa"},
            momento=agora,
        )
        return ResultadoTurno.IGNORADO
    if _ja_pediu_texto(sessao, conversa.id, deps.modo):
        rotulo = rotulo_humano(MotivoHumano.AUDIO)
        if deps.modo is Modo.SOMBRA:
            fechar_turno(
                sessao, turno.id, ResultadoTurno.SOMBRA, acao_principal=rotulo, momento=agora
            )
            return ResultadoTurno.SOMBRA
        _handoff(sessao, conversa, MotivoHumano.AUDIO, deps, agora)
        fechar_turno(
            sessao,
            turno.id,
            ResultadoTurno.HANDOFF,
            acao_principal=rotulo,
            redacao={"saida": "HANDOFF"},
            momento=agora,
        )
        return ResultadoTurno.HANDOFF
    if deps.modo is Modo.SOMBRA:
        fechar_turno(
            sessao,
            turno.id,
            ResultadoTurno.SOMBRA,
            acao_principal=ROTULO_PEDIR_TEXTO,
            momento=agora,
        )
        return ResultadoTurno.SOMBRA
    politica = deps.politica
    texto = mensagem_fixa(
        TipoFixa.PEDIR_TEXTO,
        DadosFixa(
            nome=conversa.cobranca.cliente.nome,
            valor_centavos=None,
            link_cartao=None,
            link_canal_externo="",
            texto=(
                politica.pedir_texto_audio_longo
                if pedido == LONGO_DEMAIS
                else politica.pedir_texto_audio_falhou
            ),
        ),
    ).texto
    _gravar_envio(sessao, conversa, texto, agora, conta_no_teto=False)
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.FIXA,
        acao_principal=ROTULO_PEDIR_TEXTO,
        texto_enviado=texto,
        redacao={"saida": "FIXA", "pedido": pedido},
        momento=agora,
    )
    return ResultadoTurno.FIXA


def _pagar_pix(
    sessao: Session, conversa: Conversa, turno: Turno, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """O toque no botão "Pagar com Pix" (D26): GENERATE_PIX direto, sem o Jev 1 nem o Jev 2.

    O pedido é inequívoco, então não há leitura nem decisão. O resto é o turno de sempre: o
    backend gera (ou reaproveita) o Pix integral, o LLM redige, o Jev 3 valida e a mensagem conta
    no teto. Em sombra, só a prévia é gravada.
    """
    avaliacao = Avaliacao(None, None, Acao.GENERATE_PIX, [], None)
    cobranca = conversa.cobranca
    if deps.modo is Modo.SOMBRA:
        previstos = numeros_previstos(avaliacao, conversa, cobranca, deps.politica)
        return _fechar(sessao, turno, ResultadoTurno.SOMBRA, avaliacao, previstos, agora)
    historico = _historico(sessao, conversa.id)
    estado = estado_jev(conversa, cobranca, historico, deps.politica)
    return _agir(sessao, conversa, turno, avaliacao, estado, historico, deps, {}, agora)


def _desistir(
    sessao: Session,
    conversa: Conversa,
    deps: Dependencias,
    agora: datetime,
    motivo: MotivoHumano = MotivoHumano.INTEGRACAO_INDISPONIVEL,
) -> ResultadoTurno:
    """Esgotadas as tentativas, com a integração fora ou com um erro de código, a conversa sai para
    o humano, uma vez só."""
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
            confirmar_envio(sessao, pendente, chatwoot_id)
    mensagens = tomar_turno(sessao, conversa.id)
    turno = abrir_turno(sessao, conversa.id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    if not humano_na_conversa(conversa) and conversa.cobranca.status is StatusCobranca.ATIVA:
        _handoff(sessao, conversa, motivo, deps, agora)
    conversa.falhas_turno = 0
    conversa.erros_turno = 0
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.HANDOFF,
        redacao={"saida": "HANDOFF", "erro": motivo.value},
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
        except _FALHAS_DE_INTEGRACAO:
            log.exception("o handoff da conversa %s também falhou", conversa_id)
    conversa.turno_em = agora + deps.politica.espera_integracao(conversa.falhas_turno)
    sessao.flush()
    return ResultadoTurno.ERRO


def _sair_da_fila(
    sessao: Session, conversa: Conversa, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Em sombra, esgotadas as tentativas, o turno é registrado com o erro e sai da fila.

    Sem handoff nem envio: a sombra não muda o estado real. A rajada é consumida.
    """
    mensagens = tomar_turno(sessao, conversa.id)
    turno = abrir_turno(sessao, conversa.id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    conversa.erros_turno = 0
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.ERRO,
        redacao={"erro": MotivoHumano.ERRO_INTERNO.value, "saida": "FILA"},
        momento=agora,
    )
    return ResultadoTurno.ERRO


def _encerrar_por_erro_interno(
    sessao: Session, conversa: Conversa, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Esgotadas as tentativas com o erro de código: o humano assume, ou a sombra sai da fila.

    Se o encerramento também quebra, a conversa sai da fila e fica com `erros_turno` no teto: o
    turno com defeito não roda de novo, e só uma mensagem nova do cliente tenta o encerramento. Com
    o Chatwoot fora, o encerramento espera como o handoff das integrações.
    """
    try:
        with sessao.begin_nested():
            if deps.modo is Modo.ATIVO:
                return _desistir(sessao, conversa, deps, agora, MotivoHumano.ERRO_INTERNO)
            return _sair_da_fila(sessao, conversa, deps, agora)
    except _FALHAS_DE_INTEGRACAO:
        log.exception(
            "o handoff por erro interno da conversa %s falhou; tenta de novo", conversa.id
        )
        conversa.erros_turno += 1
        conversa.turno_em = agora + deps.politica.espera_integracao(conversa.erros_turno)
    except Exception:
        log.exception(
            "o encerramento por erro interno da conversa %s também falhou; ela sai da fila e só "
            "uma mensagem nova tenta de novo",
            conversa.id,
        )
        conversa.turno_em = None
    sessao.flush()
    return ResultadoTurno.ERRO


def _erro_interno(
    sessao: Session, conversa_id: int, erro: Exception, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Um erro de código no turno: desfaz o turno, conta e volta para a fila ou encerra.

    O rollback desfaz o que o turno gravou depois do último commit; a contagem e o registro do erro
    vêm depois dele, por isso sobrevivem. Só o tipo do erro vai ao banco: a mensagem pode trazer
    URL ou matrícula. O stack trace fica no log.
    """
    sessao.rollback()
    conversa = sessao.get_one(Conversa, conversa_id)
    conversa.erros_turno += 1
    log.error(
        "o turno da conversa %s falhou por erro de código (%s de %s)",
        conversa_id,
        conversa.erros_turno,
        deps.politica.erro_interno_tentativas,
        exc_info=erro,
    )
    if conversa.erros_turno >= deps.politica.erro_interno_tentativas:
        return _encerrar_por_erro_interno(sessao, conversa, deps, agora)
    mensagens = mensagens_pendentes(sessao, conversa_id)
    turno = abrir_turno(sessao, conversa_id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.ERRO,
        redacao={"erro": type(erro).__name__, "tentativa": conversa.erros_turno},
        momento=agora,
    )
    conversa.turno_em = agora + deps.politica.espera_integracao(conversa.erros_turno)
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
    except _FALHAS_DE_INTEGRACAO as erro:
        log.exception("o envio da conversa %s falhou", conversa.id)
        return _registrar_falha(sessao, conversa.id, erro, deps, agora, desistindo=False)
    conversa.falhas_turno = 0
    _limpar_retorno(sessao, conversa, agora, retorno)
    sessao.flush()
    return ResultadoTurno.ENVIADO


def _retomar_envio(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, deps: Dependencias, agora: datetime
) -> ResultadoTurno:
    """Um turno anterior gravou a mensagem e não a confirmou: refaz só o envio, não o turno.

    A mensagem passa de novo pelos gates, porque a conversa pode ter mudado na espera.
    """
    gate = avaliar_envio(conversa, mensagem, agora, deps.politica, sessao)
    if isinstance(gate, Adiar):
        agendar_turno(sessao, conversa.id, gate.ate)
        return ResultadoTurno.ADIADO
    if not isinstance(gate, Seguir):
        # O humano assumiu, a cobrança fechou, passaram as 24h ou o teto fechou: o texto não sai.
        _descartar(sessao, mensagem)
        conversa.falhas_turno = 0
        _limpar_retorno(sessao, conversa, agora, None)
        return ResultadoTurno.IGNORADO
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
    except _FALHAS_DE_INTEGRACAO as erro:
        log.exception("o turno da conversa %s falhou", conversa.id)
        return _registrar_falha(sessao, conversa.id, erro, deps, agora, desistindo=desistindo)
    # Só a mensagem que este turno acabou de gravar vai agora. A que o `_desistir` adiou fica
    # PENDENTE até a janela abrir, e ele a confere antes de descartar ou adotar.
    pendente = None if desistindo else _envio_pendente(sessao, conversa.id)
    if pendente is None:
        if conversa.falhas_turno and resultado is not ResultadoTurno.ADIADO:
            conversa.falhas_turno = 0
            sessao.flush()
        return resultado
    retorno = agora + deps.politica.espera_integracao(1)
    conversa.turno_em = retorno
    sessao.commit()
    entrega = _entregar(sessao, conversa, pendente, deps, agora, reenvio=False, retorno=retorno)
    # Saiu: o resultado é o do turno gravado (o pedido de texto é FIXA, não ENVIADO).
    return resultado if entrega is ResultadoTurno.ENVIADO else entrega


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

    anterior = conversa.ultima_tomada_id
    mensagens = tomar_turno(sessao, conversa_id)
    if (
        isinstance(gate, Seguir)
        and deps.transcritor is not None
        and audios_sem_resultado(mensagens)
    ):
        # Um áudio chegou enquanto os outros eram transcritos: devolve a rajada e volta já, para
        # ouvi-lo. Lido sem transcrição, ele valeria como "não ouvido" e ninguém o tentaria mais.
        conversa.ultima_tomada_id = anterior
        agendar_turno(sessao, conversa_id, agora)
        return ResultadoTurno.ADIADO
    turno = abrir_turno(sessao, conversa_id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)

    def fechar(resultado: ResultadoTurno) -> ResultadoTurno:
        fechar_turno(sessao, turno.id, resultado, acao_principal=_rotulo(gate), momento=agora)
        return resultado

    if isinstance(gate, Ignorar):
        return fechar(ResultadoTurno.IGNORADO)
    if isinstance(gate, Seguir):
        pedido = pedido_de_texto(mensagens)
        if pedido is not None:
            return _pedir_texto(sessao, conversa, turno, pedido, deps, agora)
        return _seguir(sessao, conversa, turno, deps, agora)
    if isinstance(gate, PagarPix):
        return _pagar_pix(sessao, conversa, turno, deps, agora)
    if deps.modo is Modo.SOMBRA:
        return fechar(ResultadoTurno.SOMBRA)
    if isinstance(gate, Fixa) and gate.tipo is TipoFixa.CANAL_EXTERNO:
        if ja_saiu_pelo_teto(sessao, conversa):
            # Já recebeu a 6ª e o atendente devolveu a conversa ao robô: volta ao humano, sem
            # repetir a mensagem nem o link.
            _handoff(sessao, conversa, MotivoHumano.TETO, deps, agora, enviar_mensagem=False)
            fechar_turno(
                sessao,
                turno.id,
                ResultadoTurno.HANDOFF,
                acao_principal=rotulo_humano(MotivoHumano.TETO),
                redacao={"saida": "HANDOFF"},
                momento=agora,
            )
            return ResultadoTurno.HANDOFF
        _handoff(sessao, conversa, MotivoHumano.TETO, deps, agora)
        return fechar(ResultadoTurno.FIXA)
    if isinstance(gate, Retomar):
        _retomar(sessao, conversa, deps, agora)
        return fechar(ResultadoTurno.FIXA)
    if isinstance(gate, Reiniciar):
        _reiniciar(sessao, conversa, turno, mensagens, deps, agora)
        return fechar(ResultadoTurno.IGNORADO)
    # A confirmação de pagamento não sai dos gates do turno (tarefa 6.1).
    return fechar(ResultadoTurno.SOMBRA)


def processar_turno(
    sessao: Session, conversa_id: int, agora: datetime, deps: Dependencias
) -> ResultadoTurno:
    """Roda o turno; o erro de código tem teto (`erro_interno_tentativas`) e depois encerra."""
    efetivo = deps
    try:
        conversa = sessao.get_one(Conversa, conversa_id)
        if deps.modo is Modo.ATIVO:
            # O modo ativo vale só para a conversa liberada no politica.toml (tarefa 5.5).
            efetivo = replace(deps, modo=modo_da_conversa(conversa.cobranca, deps.politica))
        if conversa.erros_turno >= deps.politica.erro_interno_tentativas:
            # As tentativas acabaram e o encerramento ficou para depois (janela, Chatwoot fora).
            return _encerrar_por_erro_interno(sessao, conversa, efetivo, agora)
        _ouvir_os_audios(sessao, conversa, agora, efetivo)
        if efetivo.modo is Modo.ATIVO:
            resultado = _turno_ativo(sessao, conversa, agora, efetivo)
        else:
            resultado = _rodar_turno(sessao, conversa, agora, efetivo)
        if conversa.erros_turno and resultado not in (ResultadoTurno.ADIADO, ResultadoTurno.ERRO):
            conversa.erros_turno = 0
            sessao.flush()
        return resultado
    except Exception as erro:
        return _erro_interno(sessao, conversa_id, erro, efetivo, agora)


def criar_processador(deps: Dependencias) -> Callable[[Session, int, datetime], None]:
    """Adapta o turno ao laço de trabalho da fila."""

    def processar(sessao: Session, conversa_id: int, agora: datetime) -> None:
        processar_turno(sessao, conversa_id, agora, deps)

    return processar
