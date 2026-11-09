"""As baixas que o CTN registra por fora do agente (spec do CTN real, decisão 3).

O cliente que paga no boleto, no cartão ou no balcão aparece no `lancamentos_bx` do CTN, até ~2h45
depois. Esta rotina do Trabalhador (a cada 10 minutos, só com o CTN real) lê as baixas novas, grava
cada uma em `baixa_ctn` (idempotente pelo `id` da API) e confere a cobrança ATIVA do cliente:

- quitou (a conferência achou rejeitadas e nenhuma em aberto): a cobrança vira PAGA, o agente para e
  sai 1 aviso curto, de texto fixo (`texto_baixa_externa`), só dentro das 24h do WhatsApp e fora do
  teto de mensagens;
- pagou parte: os meses, a dívida e a régua saem da conferência e o agente segue, sem mensagem;
- baixa do mês corrente (ainda não vencido): ignorada, não abate;
- cobrança já PAGA (D39) ou sem cobrança ATIVA: só registrada.

O cursor (`cursor_ctn`, nome `baixas`) só anda depois de as baixas serem gravadas; se o CTN falhar,
nada anda e a rotina não levanta. Uma baixa cuja conferência não pôde rodar fica por tratar
(`aplicada_em` nulo) e é refeita no ciclo seguinte, até `PRAZO_DA_CONFERENCIA`. Um Pix nosso
pendente não é tocado: se for pago depois, cai no fluxo do pagamento em dobro (D39).

Cada cobrança é commitada sozinha, e o commit vem antes de qualquer envio: o que já saiu não se
perde se a seguinte quebrar, e a conferência (que chama o CTN) nunca segura uma trava de linha.
Nenhum log leva matrícula, nome nem telefone: só ids do banco.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agente.carteira.conferir import conferir_cobranca
from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import BaixaCTNRegistro, Cliente, Cobranca, Conversa, CursorCTN, Mensagem
from agente.integracoes.chatwoot import Chatwoot
from agente.integracoes.ctn import CTN, BaixaCTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.pagamento.confirmar import dentro_das_24h
from agente.politica import Politica
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    Modo,
    OrigemMensagem,
    StatusCobranca,
)
from agente.turno.entrega import confirmar_envio, entregue
from agente.turno.gates import humano_na_conversa
from agente.turno.liberacao import modo_da_conversa

log = logging.getLogger(__name__)

CURSOR_BAIXAS = "baixas"
# A API leva até ~2h45 para mostrar a baixa: sem cursor, a 1ª leitura recua mais que isso.
RECUO_DA_PRIMEIRA_LEITURA = timedelta(hours=3)
# Quanto antes do cursor guardado cada leitura recomeça, para pegar a baixa que a API mostra
# atrasada (com `coletado_em` anterior ao cursor). Começa em 0: o atraso de até 2h45 entre a
# `coletado_em` e a baixa aparecer será medido no ensaio real, e então este valor se ajusta AQUI.
# As baixas relidas já gravadas vão em `vistos` e não custam nada além das linhas lidas; mas
# uma margem que cubra mais de 50 baixas tira a leitura da 1ª página (ver `CTNEficaz`).
MARGEM_DE_RELEITURA = timedelta(0)
# A baixa que a conferência não consegue tratar é refeita a cada ciclo; passado este prazo desde a
# coleta, desiste (`nao_conferida`) em vez de gastar chamadas para sempre.
PRAZO_DA_CONFERENCIA = timedelta(hours=24)
MOTIVO_BAIXA_EXTERNA = "baixa_externa"

# O que a rotina fez com a baixa (`baixa_ctn.efeito`).
QUITOU = "quitou"  # a cobrança virou PAGA e o aviso saiu
QUITOU_AVISO_PENDENTE = "quitou_aviso_pendente"  # PAGA; o aviso está gravado e o Chatwoot falhou
QUITOU_FORA_DA_JANELA = "quitou_fora_da_janela"  # PAGA; passadas as 24h, o aviso não sai
QUITOU_SEM_CONVERSA = "quitou_sem_conversa"  # PAGA; o cliente nunca foi contatado
QUITOU_SOMBRA = "quitou_sombra"  # PAGA; em sombra (ou fora da liberação) nada é enviado
QUITOU_COM_HUMANO = "quitou_com_humano"  # PAGA; o atendente soube pela nota, o cliente não recebe
ABATEU = "abateu"  # pagou parte: os meses em aberto diminuíram
SEM_MUDANCA = "sem_mudanca"  # conferida, e os meses em aberto continuam os mesmos
IGNORADA = "ignorada"  # o mês corrente, ainda não vencido
JA_PAGA = "ja_paga"  # a cobrança já estava PAGA (D39)
SEM_COBRANCA = "sem_cobranca"  # o cliente não tem cobrança ATIVA
CONFERIDA_JUNTO = "conferida_junto"  # outra baixa da mesma matrícula, na mesma conferência
NAO_CONFERIDA = "nao_conferida"  # a conferência não rodou em `PRAZO_DA_CONFERENCIA`

_EFEITOS_IGNORADOS = {IGNORADA, JA_PAGA, SEM_COBRANCA, SEM_MUDANCA}


@dataclass(frozen=True)
class DependenciasDasBaixas:
    """O que a rotina usa: a política, o Chatwoot, o CTN e o modo do serviço."""

    politica: Politica
    chatwoot: Chatwoot
    time_humano: int | None
    modo: Modo
    ctn: CTN


@dataclass
class ResumoBaixas:
    """Os totais de uma passada; só contagens. `lidas` são as baixas novas gravadas (as que o
    cursor inclusivo devolve de novo não contam); `aplicadas` são as que ganharam um efeito
    nesta passada; `ignoradas`, as que não mudaram nada (mês corrente, cobrança já paga ou
    inexistente, ou conferência sem mudança)."""

    lidas: int = 0
    aplicadas: int = 0
    quitadas: int = 0
    abatidas: int = 0
    ignoradas: int = 0


def _cursor(sessao: Session, agora: datetime) -> datetime:
    registro = sessao.get(CursorCTN, CURSOR_BAIXAS)
    if registro is None:
        return agora - RECUO_DA_PRIMEIRA_LEITURA
    return datetime.fromisoformat(registro.valor)


def _como_utc(instante: datetime) -> datetime:
    return instante if instante.tzinfo is not None else instante.replace(tzinfo=UTC)


def _vistos(sessao: Session, a_partir_de: datetime) -> set[str]:
    """Os ids das baixas já gravadas com `coletado_em` a partir do cursor: a leitura não as traz de
    volta e, por elas, sabe que as linhas do instante do cursor já foram vistas. Vêm do próprio
    `baixa_ctn` (gravado na mesma transação que o cursor), então o cursor continua sendo só uma
    data e o valor de antes segue valendo."""
    folga = a_partir_de - timedelta(days=1)  # a comparação exata é feita aqui, não no banco
    linhas = sessao.execute(
        select(BaixaCTNRegistro.id_externo, BaixaCTNRegistro.coletado_em).where(
            BaixaCTNRegistro.coletado_em >= folga
        )
    )
    return {id_externo for id_externo, coletado in linhas if _como_utc(coletado) >= a_partir_de}


def _gravar_cursor(sessao: Session, coletado_em: datetime) -> None:
    valor = coletado_em.astimezone(UTC).isoformat()
    registro = sessao.get(CursorCTN, CURSOR_BAIXAS)
    if registro is None:
        sessao.add(CursorCTN(nome=CURSOR_BAIXAS, valor=valor))
    elif datetime.fromisoformat(registro.valor) < coletado_em:
        registro.valor = valor
    sessao.flush()


def _gravar_baixas(sessao: Session, baixas: list[BaixaCTN]) -> int:
    """Grava as baixas que o banco ainda não tem e devolve quantas eram novas."""
    conhecidas: set[str] = set()
    ids = [baixa.id_externo for baixa in baixas]
    for inicio in range(0, len(ids), 500):
        conhecidas.update(
            sessao.scalars(
                select(BaixaCTNRegistro.id_externo).where(
                    BaixaCTNRegistro.id_externo.in_(ids[inicio : inicio + 500])
                )
            )
        )
    novas = 0
    for baixa in baixas:
        if baixa.id_externo in conhecidas:
            continue
        conhecidas.add(baixa.id_externo)
        try:
            # Outra instância pode ter gravado a mesma baixa entre a leitura e agora.
            with sessao.begin_nested():
                sessao.add(
                    BaixaCTNRegistro(
                        id_externo=baixa.id_externo,
                        matricula=baixa.matricula,
                        mes_ref=baixa.mes_ref,
                        valor_centavos=baixa.valor_centavos,
                        forma=baixa.forma,
                        pago_em=baixa.pago_em,
                        coletado_em=baixa.coletado_em,
                    )
                )
        except IntegrityError:
            continue
        novas += 1
    return novas


def _ler_do_ctn(sessao: Session, ctn: CTN, agora: datetime, resumo: ResumoBaixas) -> bool:
    """Lê as baixas desde o cursor e as grava. False se o CTN falhou (nada andou)."""
    cursor = _cursor(sessao, agora) - MARGEM_DE_RELEITURA
    try:
        baixas = ctn.baixas_desde(cursor, _vistos(sessao, cursor))
    except (CTNIndisponivel, ClienteNaoEncontrado, httpx.HTTPError) as erro:
        log.warning(
            "a leitura das baixas do CTN falhou (%s); o cursor não anda", type(erro).__name__
        )
        return False
    resumo.lidas = _gravar_baixas(sessao, baixas)
    if baixas:
        # Nunca além de `agora`: o relógio errado do coletor (`coletado_em` no futuro) levaria o
        # cursor junto e as baixas coletadas de agora até aquela data nunca seriam lidas.
        maior = max(baixa.coletado_em for baixa in baixas)
        if maior > agora:
            log.warning("o CTN trouxe baixa com coletado_em no futuro; o cursor fica em agora")
        _gravar_cursor(sessao, min(maior, agora))
    sessao.commit()
    return True


def _mes_corrente(politica: Politica, agora: datetime) -> str:
    local = agora.astimezone(politica.janela.zona)
    return f"{local.year:04d}/{local.month:02d}"


def _cobranca_do_cliente(sessao: Session, matricula: str) -> Cobranca | None:
    """A cobrança ATIVA do cliente; sem ela, a mais recente (para saber se já foi paga)."""
    cobrancas = sessao.scalars(
        select(Cobranca)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(Cliente.matricula_ctn == matricula)
        .order_by(Cobranca.id.desc())
    ).all()
    ativa = next((c for c in cobrancas if c.status is StatusCobranca.ATIVA), None)
    return ativa or (cobrancas[0] if cobrancas else None)


def _conversa_mais_recente(cobranca: Cobranca) -> Conversa | None:
    return max(cobranca.conversas, key=lambda conversa: conversa.id) if cobranca.conversas else None


def _marcar(
    resumo: ResumoBaixas,
    baixa: BaixaCTNRegistro,
    efeito: str,
    cobranca_id: int | None,
    agora: datetime,
) -> None:
    baixa.efeito = efeito
    baixa.cobranca_id = cobranca_id
    baixa.aplicada_em = agora
    resumo.aplicadas += 1
    if efeito in _EFEITOS_IGNORADOS:
        resumo.ignoradas += 1


def _enviar_aviso(
    sessao: Session, deps: DependenciasDasBaixas, conversa: Conversa, mensagem: Mensagem
) -> bool:
    """Envia a mensagem gravada PENDENTE (o que já saiu no Chatwoot não sai de novo). False se o
    Chatwoot falhou: ela fica PENDENTE para a próxima passada."""
    try:
        chatwoot_id = entregue(sessao, conversa, mensagem, deps.chatwoot)
        if chatwoot_id is None:
            chatwoot_id = deps.chatwoot.enviar_mensagem(
                conversa.chatwoot_conversa_id, mensagem.texto
            )
        confirmar_envio(sessao, mensagem, chatwoot_id)
    except Exception as erro:
        log.warning(
            "o aviso da baixa externa não saiu (%s); fica para a próxima", type(erro).__name__
        )
        return False
    return True


def _nota_ao_atendente(deps: DependenciasDasBaixas, conversa: Conversa) -> None:
    try:
        deps.chatwoot.nota_privada(
            conversa.chatwoot_conversa_id,
            "Cobrança quitada fora do agente: a baixa chegou pelo CTN. O robô encerrou a "
            "cobrança e não mandou mensagem ao cliente.",
        )
    except Exception as erro:
        log.warning("a nota da baixa externa ao atendente falhou (%s)", type(erro).__name__)


def _encerrar_conversa(sessao: Session, conversa: Conversa, agora: datetime) -> None:
    if conversa.estado not in (EstadoConversa.PAGO, EstadoConversa.ENCERRADO):
        transicionar(sessao, conversa, EstadoConversa.PAGO, None, None, momento=agora)
    if conversa.estado is not EstadoConversa.ENCERRADO:
        transicionar(sessao, conversa, EstadoConversa.ENCERRADO, None, None, momento=agora)


def _quitar(
    sessao: Session, deps: DependenciasDasBaixas, cobranca: Cobranca, agora: datetime
) -> tuple[str, Conversa | None, Mensagem | None]:
    """A cobrança vira PAGA e a conversa encerra. Devolve o efeito e, se o aviso foi gravado
    PENDENTE, a conversa e a mensagem a enviar depois do commit."""
    cobranca.status = StatusCobranca.PAGA
    cobranca.encerrada_motivo = MOTIVO_BAIXA_EXTERNA
    cobranca.encerrada_em = agora
    conversa = _conversa_mais_recente(cobranca)
    if conversa is None:
        return QUITOU_SEM_CONVERSA, None, None
    if deps.modo is not Modo.ATIVO or modo_da_conversa(cobranca, deps.politica) is not Modo.ATIVO:
        # A sombra não envia nem muda o estado da conversa (arquitetura, seção 8).
        return QUITOU_SOMBRA, None, None
    com_humano = humano_na_conversa(conversa)
    _encerrar_conversa(sessao, conversa, agora)
    if com_humano:
        return QUITOU_COM_HUMANO, conversa, None
    if conversa.chatwoot_conversa_id is None or not dentro_das_24h(sessao, conversa, agora):
        return QUITOU_FORA_DA_JANELA, conversa, None
    mensagem = registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.FIXA,
        deps.politica.texto_baixa_externa,
        None,
        False,
        agora,
        envio=EstadoEnvio.PENDENTE,
    )
    return QUITOU_AVISO_PENDENTE, conversa, mensagem


def _tratar_cobranca_ativa(
    sessao: Session,
    deps: DependenciasDasBaixas,
    cobranca: Cobranca,
    baixas: list[BaixaCTNRegistro],
    agora: datetime,
    resumo: ResumoBaixas,
) -> tuple[Conversa, Mensagem] | None:
    """Aplica as baixas de uma cobrança ATIVA. Devolve a conversa e a mensagem do aviso, se ele
    precisa sair depois do commit."""
    corrente = _mes_corrente(deps.politica, agora)
    a_conferir = []
    for baixa in baixas:
        if baixa.mes_ref >= corrente:
            _marcar(resumo, baixa, IGNORADA, cobranca.id, agora)
        else:
            a_conferir.append(baixa)
    if not a_conferir:
        return None

    antes = cobranca.mensalidades_abertas
    # As baixas da pessoa que um turno leu há até 60 s ficam em cache no cliente do CTN: a
    # conferência daqui, que existe por causa de uma baixa nova, lê de novo.
    deps.ctn.invalidar_cache(a_conferir[0].matricula)
    # A conferência chama o CTN: nada pendente na sessão e nenhuma trava de linha durante a chamada.
    sessao.commit()
    conferencia = conferir_cobranca(sessao, deps.ctn, deps.politica, cobranca.id, agora)
    if not conferencia.conferida:
        # O CTN caiu, a cobrança fechou no meio ou o dado não é crível: a baixa fica por tratar.
        for baixa in a_conferir:
            if agora - baixa.coletado_em >= PRAZO_DA_CONFERENCIA:
                _marcar(resumo, baixa, NAO_CONFERIDA, cobranca.id, agora)
        return None

    principal, *outras = a_conferir
    mensagem = conversa = None
    if conferencia.quitada_no_ctn:
        efeito, conversa, mensagem = _quitar(sessao, deps, cobranca, agora)
        resumo.quitadas += 1
    elif len(conferencia.meses_em_aberto) < antes:
        efeito = ABATEU
        resumo.abatidas += 1
    else:
        efeito = SEM_MUDANCA
    _marcar(resumo, principal, efeito, cobranca.id, agora)
    for baixa in outras:
        _marcar(resumo, baixa, CONFERIDA_JUNTO, cobranca.id, agora)
    if efeito == QUITOU_COM_HUMANO:
        _nota_ao_atendente(deps, conversa)
    return (conversa, mensagem) if mensagem is not None else None


def _tratar_matricula(
    sessao: Session,
    deps: DependenciasDasBaixas,
    matricula: str,
    baixas: list[BaixaCTNRegistro],
    agora: datetime,
    resumo: ResumoBaixas,
) -> None:
    cobranca = _cobranca_do_cliente(sessao, matricula)
    if cobranca is None or cobranca.status not in (StatusCobranca.ATIVA, StatusCobranca.PAGA):
        for baixa in baixas:
            _marcar(resumo, baixa, SEM_COBRANCA, None, agora)
        return
    if cobranca.status is StatusCobranca.PAGA:
        for baixa in baixas:
            _marcar(resumo, baixa, JA_PAGA, cobranca.id, agora)
        return
    aviso = _tratar_cobranca_ativa(sessao, deps, cobranca, baixas, agora, resumo)
    sessao.commit()
    if aviso is not None:
        conversa, mensagem = aviso
        principal = next(b for b in baixas if b.efeito == QUITOU_AVISO_PENDENTE)
        if _enviar_aviso(sessao, deps, conversa, mensagem):
            principal.efeito = QUITOU
        sessao.commit()


def _tratar_pendentes(
    sessao: Session, deps: DependenciasDasBaixas, agora: datetime, resumo: ResumoBaixas
) -> None:
    pendentes = sessao.scalars(
        select(BaixaCTNRegistro)
        .where(BaixaCTNRegistro.aplicada_em.is_(None))
        .order_by(BaixaCTNRegistro.coletado_em, BaixaCTNRegistro.id)
    ).all()
    por_matricula: dict[str, list[BaixaCTNRegistro]] = defaultdict(list)
    for baixa in pendentes:
        por_matricula[baixa.matricula].append(baixa)
    for matricula, baixas in por_matricula.items():
        ids = [baixa.id for baixa in baixas]
        try:
            _tratar_matricula(sessao, deps, matricula, baixas, agora, resumo)
            sessao.commit()
        except Exception:
            sessao.rollback()
            log.exception("as baixas %s do CTN não puderam ser tratadas", ids)


def _reenviar_avisos(sessao: Session, deps: DependenciasDasBaixas, agora: datetime) -> None:
    """O aviso gravado PENDENTE que o Chatwoot não recebeu sai agora; se as 24h passaram, é
    descartado."""
    baixas = sessao.scalars(
        select(BaixaCTNRegistro).where(BaixaCTNRegistro.efeito == QUITOU_AVISO_PENDENTE)
    ).all()
    for baixa in baixas:
        cobranca = sessao.get(Cobranca, baixa.cobranca_id)
        conversa = _conversa_mais_recente(cobranca) if cobranca is not None else None
        mensagem = (
            sessao.scalars(
                select(Mensagem)
                .where(
                    Mensagem.conversa_id == conversa.id,
                    Mensagem.envio == EstadoEnvio.PENDENTE,
                    Mensagem.origem == OrigemMensagem.FIXA,
                    Mensagem.texto == deps.politica.texto_baixa_externa,
                )
                .order_by(Mensagem.id.desc())
            ).first()
            if conversa is not None
            else None
        )
        if mensagem is None:
            baixa.efeito = QUITOU_FORA_DA_JANELA
        elif not dentro_das_24h(sessao, conversa, agora):
            mensagem.envio = EstadoEnvio.DESCARTADA
            baixa.efeito = QUITOU_FORA_DA_JANELA
        elif _enviar_aviso(sessao, deps, conversa, mensagem):
            baixa.efeito = QUITOU
        sessao.commit()


def ler_baixas(sessao: Session, deps: DependenciasDasBaixas, agora: datetime) -> ResumoBaixas:
    """Uma passada da rotina: reenvia os avisos que ficaram pendentes, lê e grava as baixas novas e
    trata as por tratar. Nunca levanta por falha do CTN ou do Chatwoot."""
    resumo = ResumoBaixas()
    _reenviar_avisos(sessao, deps, agora)
    if not _ler_do_ctn(sessao, deps.ctn, agora, resumo):
        return resumo
    _tratar_pendentes(sessao, deps, agora, resumo)
    return resumo
