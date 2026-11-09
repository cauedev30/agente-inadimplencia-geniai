"""As baixas que o CTN registra por fora do agente (spec do CTN real, decisão 3).

O cliente que paga no boleto, no cartão ou no balcão aparece no `lancamentos_bx` do CTN, até ~2h45
depois. Esta rotina do Trabalhador (a cada 10 minutos, só com o CTN real) lê as baixas novas, grava
cada uma em `baixa_ctn` (idempotente pelo `id` da API) e confere a cobrança ATIVA do cliente:

- quitou (a conferência achou rejeitadas e nenhuma em aberto): a cobrança vira PAGA, o agente para e
  sai 1 aviso curto, de texto fixo (`texto_baixa_externa`), só dentro das 24h do WhatsApp e fora do
  teto de mensagens;
- pagou parte: os meses, a dívida e a régua saem da conferência e o agente segue, sem mensagem;
- baixa do mês corrente (ou futuro) que NÃO está entre os meses em aberto da cobrança: o mês ainda
  não venceu, é ignorada e não abate. Se o mês corrente está em atraso (`meses_em_aberto` o traz),
  ou a cobrança ainda não tem a lista, a baixa é conferida como qualquer outra;
- cobrança já PAGA (D39) ou sem cobrança ATIVA: só registrada.

O cursor (`cursor_ctn`, nome `baixas`) só anda depois de as baixas serem gravadas; se o CTN ou a
gravação falharem, nada anda e a rotina não levanta. Uma baixa cuja conferência não pôde rodar
porque o CTN estava fora fica por tratar (`aplicada_em` nulo) e é refeita no ciclo seguinte, até
`PRAZO_DA_CONFERENCIA`; sem lançamentos ou com dado suspeito, a resposta não muda numa nova
tentativa, e a baixa encerra na hora como `nao_conferida`. Por passada, a rotina confere no máximo
`MAX_CONFERENCIAS_POR_CICLO` matrículas e para na primeira vez que o CTN estiver fora.

No abate, o Pix e o link pendentes de valor diferente da dívida nova expiram (`expirar.py`); o Pix
nosso pendente de valor igual não é tocado, e na quitação também não: se for pago depois, cai no
fluxo do pagamento em dobro (D39).

Cada cobrança é commitada sozinha, e o commit vem antes de qualquer envio ou nota ao atendente: o
Chatwoot lento não segura trava de linha, e o que já saiu não se perde se a seguinte quebrar. A
conferência (que chama o CTN) também nunca segura uma trava durante a chamada; depois dela, a
gravação trava as conversas e a cobrança na ordem de `db/travas.py`, a mesma do webhook do Pix. O
reenvio do aviso repete os testes do 1º envio (modo, piloto, atendente, 24h). Nenhum log leva
matrícula, nome nem telefone, nem o texto do erro do SQL (que traz os parâmetros): só o tipo do
erro e ids do banco.
"""

import hashlib
import logging
from dataclasses import dataclass, fields, replace
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
from agente.pagamento.confirmar import MOTIVO_BAIXA_EXTERNA, dentro_das_24h
from agente.pagamento.expirar import expirar_de_outro_valor
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
# Quantas matrículas a rotina confere no CTN por passada (2 chamadas cada, no mesmo fio dos turnos,
# com 60 chamadas por minuto de cota). As que passam do teto ficam por tratar para o ciclo seguinte.
MAX_CONFERENCIAS_POR_CICLO = 10

# O que a rotina fez com a baixa (`baixa_ctn.efeito`).
QUITOU = "quitou"  # a cobrança virou PAGA e o aviso saiu
QUITOU_AVISO_PENDENTE = "quitou_aviso_pendente"  # PAGA; o aviso está gravado e o Chatwoot falhou
QUITOU_FORA_DA_JANELA = "quitou_fora_da_janela"  # PAGA; passadas as 24h, o aviso não sai
QUITOU_SEM_CONVERSA = "quitou_sem_conversa"  # PAGA; o cliente nunca foi contatado
QUITOU_SOMBRA = "quitou_sombra"  # PAGA; em sombra (ou fora da liberação) nada é enviado
QUITOU_COM_HUMANO = "quitou_com_humano"  # PAGA; o atendente soube pela nota, o cliente não recebe
# PAGA; a nota ao atendente está por enviar (o Chatwoot falhou, ou ele assumiu depois do aviso)
QUITOU_COM_HUMANO_NOTA_PENDENTE = "quitou_com_humano_nota_pendente"
QUITOU_COM_HUMANO_SEM_NOTA = "quitou_com_humano_sem_nota"  # PAGA; a nota não saiu em 24h
ABATEU = "abateu"  # pagou parte: os meses em aberto diminuíram
SEM_MUDANCA = "sem_mudanca"  # conferida, e os meses em aberto continuam os mesmos
IGNORADA = "ignorada"  # o mês corrente, ainda não vencido (fora dos meses em aberto)
JA_PAGA = "ja_paga"  # a cobrança já estava PAGA (D39)
SEM_COBRANCA = "sem_cobranca"  # o cliente não tem cobrança ATIVA
CONFERIDA_JUNTO = "conferida_junto"  # outra baixa da mesma matrícula, na mesma conferência
NAO_CONFERIDA = "nao_conferida"  # a conferência não rodou em `PRAZO_DA_CONFERENCIA`
# A API trouxe a baixa com um campo acima da coluna: fica só o rastro, sem tratamento.
MALFORMADA = "malformada"

_EFEITOS_IGNORADOS = {IGNORADA, JA_PAGA, SEM_COBRANCA, SEM_MUDANCA}
# Os efeitos com um envio por fazer: a rotina os refaz a cada passada.
_COM_ENVIO_POR_FAZER = (QUITOU_AVISO_PENDENTE, QUITOU_COM_HUMANO_NOTA_PENDENTE)


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
    inexistente, ou conferência sem mudança); `puladas`, as baixas novas malformadas, que deixam
    só o rastro de efeito `malformada` em `baixa_ctn`."""

    lidas: int = 0
    aplicadas: int = 0
    quitadas: int = 0
    abatidas: int = 0
    ignoradas: int = 0
    puladas: int = 0


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


_COLUNAS = BaixaCTNRegistro.__table__.c
_MAIOR_INTEIRO = 2**31 - 1


def _cabe_no_banco(baixa: BaixaCTN) -> bool:
    """Um campo acima da coluna dá `DataError` no Postgres e travaria a rotina a cada ciclo."""
    return (
        len(baixa.id_externo) <= _COLUNAS.id_externo.type.length
        and len(baixa.matricula) <= _COLUNAS.matricula.type.length
        and len(baixa.mes_ref) <= _COLUNAS.mes_ref.type.length
        and abs(baixa.valor_centavos) <= _MAIOR_INTEIRO
    )


def _rastro_da_malformada(baixa: BaixaCTN, agora: datetime) -> BaixaCTNRegistro:
    """A linha que a baixa malformada deixa em `baixa_ctn`: o `id` da API não cabe na coluna, então
    o `id_externo` é um resumo dele (estável: a leitura repetida não grava de novo) e os campos
    longos vão cortados. `aplicada_em` preenchido a tira da fila das por tratar."""
    resumo = hashlib.sha256(baixa.id_externo.encode()).hexdigest()[:32]
    return BaixaCTNRegistro(
        id_externo=f"{MALFORMADA}:{resumo}",
        matricula=baixa.matricula[: _COLUNAS.matricula.type.length],
        mes_ref=baixa.mes_ref[: _COLUNAS.mes_ref.type.length],
        valor_centavos=max(-_MAIOR_INTEIRO, min(_MAIOR_INTEIRO, baixa.valor_centavos)),
        forma=baixa.forma,
        pago_em=baixa.pago_em,
        coletado_em=baixa.coletado_em,
        efeito=MALFORMADA,
        aplicada_em=agora,
    )


def _conhecidas(sessao: Session, ids: list[str]) -> set[str]:
    conhecidas: set[str] = set()
    for inicio in range(0, len(ids), 500):
        conhecidas.update(
            sessao.scalars(
                select(BaixaCTNRegistro.id_externo).where(
                    BaixaCTNRegistro.id_externo.in_(ids[inicio : inicio + 500])
                )
            )
        )
    return conhecidas


def _adicionar(sessao: Session, registro: BaixaCTNRegistro) -> bool:
    """False se outra instância gravou a mesma baixa entre a leitura e agora."""
    try:
        with sessao.begin_nested():
            sessao.add(registro)
    except IntegrityError:
        return False
    return True


def _gravar_baixas(
    sessao: Session, baixas: list[BaixaCTN], agora: datetime, resumo: ResumoBaixas
) -> int:
    """Grava as baixas que o banco ainda não tem e devolve quantas eram novas. A baixa com um
    campo que não cabe na coluna não é gravada como as outras (o cursor passa por ela): deixa o
    rastro de efeito `malformada` (uma vez só) e entra em `resumo.puladas`; o log leva só a
    contagem."""
    validas = [baixa for baixa in baixas if _cabe_no_banco(baixa)]
    malformadas = [baixa for baixa in baixas if not _cabe_no_banco(baixa)]
    if malformadas:
        log.warning("%s baixas do CTN têm campo acima da coluna e foram puladas", len(malformadas))
        rastros = {baixa.id_externo: _rastro_da_malformada(baixa, agora) for baixa in malformadas}
        conhecidas = _conhecidas(sessao, [r.id_externo for r in rastros.values()])
        for registro in rastros.values():
            if registro.id_externo not in conhecidas and _adicionar(sessao, registro):
                resumo.puladas += 1
    conhecidas = _conhecidas(sessao, [baixa.id_externo for baixa in validas])
    novas = 0
    for baixa in validas:
        if baixa.id_externo in conhecidas:
            continue
        conhecidas.add(baixa.id_externo)
        registro = BaixaCTNRegistro(
            id_externo=baixa.id_externo,
            matricula=baixa.matricula,
            mes_ref=baixa.mes_ref,
            valor_centavos=baixa.valor_centavos,
            forma=baixa.forma,
            pago_em=baixa.pago_em,
            coletado_em=baixa.coletado_em,
        )
        if _adicionar(sessao, registro):
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
    novas = _gravar_baixas(sessao, baixas, agora, resumo)
    if baixas:
        # Nunca além de `agora`: o relógio errado do coletor (`coletado_em` no futuro) levaria o
        # cursor junto e as baixas coletadas de agora até aquela data nunca seriam lidas.
        maior = max(baixa.coletado_em for baixa in baixas)
        if maior > agora:
            log.warning("o CTN trouxe baixa com coletado_em no futuro; o cursor fica em agora")
        _gravar_cursor(sessao, min(maior, agora))
    sessao.commit()
    resumo.lidas = novas
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


def _enviar_nota(deps: DependenciasDasBaixas, conversa: Conversa) -> bool:
    """A nota privada ao atendente, depois do commit. False se o Chatwoot falhou."""
    try:
        deps.chatwoot.nota_privada(
            conversa.chatwoot_conversa_id,
            "Cobrança quitada fora do agente: a baixa chegou pelo CTN. O robô encerrou a "
            "cobrança e não mandou mensagem ao cliente.",
        )
    except Exception as erro:
        log.warning("a nota da baixa externa ao atendente falhou (%s)", type(erro).__name__)
        return False
    return True


def _encerrar_conversa(sessao: Session, conversa: Conversa, agora: datetime) -> None:
    if conversa.estado not in (EstadoConversa.PAGO, EstadoConversa.ENCERRADO):
        transicionar(sessao, conversa, EstadoConversa.PAGO, None, None, momento=agora)
    if conversa.estado is not EstadoConversa.ENCERRADO:
        transicionar(sessao, conversa, EstadoConversa.ENCERRADO, None, None, momento=agora)


def _em_sombra(deps: DependenciasDasBaixas, cobranca: Cobranca) -> bool:
    """O serviço está em sombra, ou o telefone não está no piloto: nada é enviado ao cliente."""
    return (
        deps.modo is not Modo.ATIVO or modo_da_conversa(cobranca, deps.politica) is not Modo.ATIVO
    )


def _quitar(
    sessao: Session, deps: DependenciasDasBaixas, cobranca: Cobranca, agora: datetime
) -> tuple[str, Conversa | None, Mensagem | None]:
    """A cobrança vira PAGA e a conversa encerra. Devolve o efeito e, se o aviso foi gravado
    PENDENTE (ou a nota ao atendente está por enviar), a conversa e a mensagem a enviar depois do
    commit."""
    cobranca.status = StatusCobranca.PAGA
    cobranca.encerrada_motivo = MOTIVO_BAIXA_EXTERNA
    cobranca.encerrada_em = agora
    conversa = _conversa_mais_recente(cobranca)
    if conversa is None:
        return QUITOU_SEM_CONVERSA, None, None
    if _em_sombra(deps, cobranca):
        # A sombra não envia nem muda o estado da conversa (arquitetura, seção 8).
        return QUITOU_SOMBRA, None, None
    com_humano = humano_na_conversa(conversa)
    _encerrar_conversa(sessao, conversa, agora)
    if com_humano:
        if conversa.chatwoot_conversa_id is None:
            return QUITOU_COM_HUMANO, conversa, None  # não há onde pôr a nota
        return QUITOU_COM_HUMANO_NOTA_PENDENTE, conversa, None
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


@dataclass
class _Tratamento:
    """O que `_tratar_cobranca_ativa` deixou para depois do commit e para o laço."""

    # Foi ao CTN conferir (gasta o teto por ciclo).
    conferiu: bool = False
    # A conferência falhou porque o CTN está fora: as próximas matrículas também falhariam.
    ctn_fora: bool = False
    # A baixa que carrega o envio por fazer (o aviso, se `mensagem`; senão, a nota ao atendente).
    principal: BaixaCTNRegistro | None = None
    conversa: Conversa | None = None
    mensagem: Mensagem | None = None


def _tratar_cobranca_ativa(
    sessao: Session,
    deps: DependenciasDasBaixas,
    cobranca: Cobranca,
    baixas: list[BaixaCTNRegistro],
    agora: datetime,
    resumo: ResumoBaixas,
) -> _Tratamento:
    """Aplica as baixas de uma cobrança ATIVA. Deixa tudo gravado e sem commit; quem chama faz o
    commit e só então envia o aviso ou a nota."""
    corrente = _mes_corrente(deps.politica, agora)
    em_aberto = cobranca.meses_em_aberto
    a_conferir = []
    for baixa in baixas:
        # O mês corrente em atraso (vencimento já passou, cartão recusado) é dívida: confere. Sem a
        # lista de meses em aberto não há como saber, e conferir é o lado seguro.
        a_vencer = (
            baixa.mes_ref >= corrente and em_aberto is not None and baixa.mes_ref not in em_aberto
        )
        if a_vencer:
            _marcar(resumo, baixa, IGNORADA, cobranca.id, agora)
        else:
            a_conferir.append(baixa)
    if not a_conferir:
        return _Tratamento()

    antes = cobranca.mensalidades_abertas
    # As baixas da pessoa que um turno leu há até 60 s ficam em cache no cliente do CTN: a
    # conferência daqui, que existe por causa de uma baixa nova, lê de novo.
    deps.ctn.invalidar_cache(a_conferir[0].matricula)
    # A conferência chama o CTN: nada pendente na sessão e nenhuma trava de linha durante a chamada.
    sessao.commit()
    # Depois da chamada, as travas na ordem do pagamento (conversas, cobrança): a quitação mexe na
    # conversa, e o webhook do Pix trava a conversa antes da cobrança (`db/travas.py`).
    conferencia = conferir_cobranca(
        sessao, deps.ctn, deps.politica, cobranca.id, agora, travar_conversas=True
    )
    if not conferencia.conferida:
        if conferencia.motivo is not None:
            # Sem lançamentos ou dado suspeito: a resposta não muda numa nova tentativa (o
            # `lancamentos` não muda no dia), e repetir só gasta a cota. Encerra já.
            for baixa in a_conferir:
                _marcar(resumo, baixa, NAO_CONFERIDA, cobranca.id, agora)
            return _Tratamento(conferiu=True)
        # O CTN caiu (ou a cobrança fechou no meio): a baixa fica por tratar até o prazo.
        for baixa in a_conferir:
            if agora - baixa.coletado_em >= PRAZO_DA_CONFERENCIA:
                _marcar(resumo, baixa, NAO_CONFERIDA, cobranca.id, agora)
        return _Tratamento(conferiu=True, ctn_fora=cobranca.status is StatusCobranca.ATIVA)

    principal, *outras = a_conferir
    mensagem = conversa = None
    if conferencia.quitada_no_ctn:
        efeito, conversa, mensagem = _quitar(sessao, deps, cobranca, agora)
        resumo.quitadas += 1
    elif len(conferencia.meses_em_aberto) < antes:
        efeito = ABATEU
        resumo.abatidas += 1
        # O Pix ou o link gerado com a dívida antiga, se fosse pago, cobriria o mês pago por fora de
        # novo.
        expirar_de_outro_valor(sessao, cobranca)
    else:
        efeito = SEM_MUDANCA
    _marcar(resumo, principal, efeito, cobranca.id, agora)
    for baixa in outras:
        _marcar(resumo, baixa, CONFERIDA_JUNTO, cobranca.id, agora)
    if efeito in _COM_ENVIO_POR_FAZER:
        return _Tratamento(conferiu=True, principal=principal, conversa=conversa, mensagem=mensagem)
    return _Tratamento(conferiu=True)


def _entregar(sessao: Session, deps: DependenciasDasBaixas, tratamento: _Tratamento) -> None:
    """Envia o aviso ou a nota que a quitação deixou por fazer (a sessão sem escrita pendente)."""
    principal, conversa = tratamento.principal, tratamento.conversa
    if tratamento.mensagem is not None:
        if _enviar_aviso(sessao, deps, conversa, tratamento.mensagem):
            principal.efeito = QUITOU
    elif _enviar_nota(deps, conversa):
        principal.efeito = QUITOU_COM_HUMANO


def _tratar_matricula(
    sessao: Session,
    deps: DependenciasDasBaixas,
    matricula: str,
    baixas: list[BaixaCTNRegistro],
    agora: datetime,
    resumo: ResumoBaixas,
) -> _Tratamento:
    cobranca = _cobranca_do_cliente(sessao, matricula)
    if cobranca is None or cobranca.status not in (StatusCobranca.ATIVA, StatusCobranca.PAGA):
        for baixa in baixas:
            _marcar(resumo, baixa, SEM_COBRANCA, None, agora)
        return _Tratamento()
    if cobranca.status is StatusCobranca.PAGA:
        for baixa in baixas:
            _marcar(resumo, baixa, JA_PAGA, cobranca.id, agora)
        return _Tratamento()
    tratamento = _tratar_cobranca_ativa(sessao, deps, cobranca, baixas, agora, resumo)
    sessao.commit()
    if tratamento.principal is not None:
        # Depois do commit: o Chatwoot lento não segura a trava da cobrança nem da conversa.
        _entregar(sessao, deps, tratamento)
        sessao.commit()
    return tratamento


def _desistir_das_vencidas(
    sessao: Session, ids: list[int], agora: datetime, resumo: ResumoBaixas
) -> None:
    """As baixas que seguem levantando erro desde antes do prazo encerram como `nao_conferida`."""
    try:
        vencidas = sessao.scalars(
            select(BaixaCTNRegistro).where(
                BaixaCTNRegistro.id.in_(ids), BaixaCTNRegistro.aplicada_em.is_(None)
            )
        ).all()
        for baixa in vencidas:
            if agora - baixa.coletado_em >= PRAZO_DA_CONFERENCIA:
                _marcar(resumo, baixa, NAO_CONFERIDA, None, agora)
        sessao.commit()
    except Exception as erro:
        sessao.rollback()
        log.error("as baixas %s não puderam ser encerradas (%s)", ids, type(erro).__name__)


def _tratar_pendentes(
    sessao: Session, deps: DependenciasDasBaixas, agora: datetime, resumo: ResumoBaixas
) -> None:
    """Trata as baixas por tratar, matrícula a matrícula, da coletada há mais tempo à mais nova.

    O teto por ciclo conta só as matrículas que foram ao CTN: a baixa de quem não está na carteira
    custa uma leitura no banco, não uma chamada. As que passam do teto ficam para o ciclo seguinte.
    No primeiro `CTNIndisponivel` a passada para: as próximas falhariam do mesmo jeito.
    """
    matriculas = dict.fromkeys(
        sessao.scalars(
            select(BaixaCTNRegistro.matricula)
            .where(BaixaCTNRegistro.aplicada_em.is_(None))
            .order_by(BaixaCTNRegistro.coletado_em, BaixaCTNRegistro.id)
        )
    )
    conferidas = 0
    for matricula in matriculas:
        if conferidas >= MAX_CONFERENCIAS_POR_CICLO:
            break
        # SKIP LOCKED (o SQLite ignora): a linha que outra instância está tratando fica com ela. A
        # trava dura até o 1º commit, que vem antes da chamada ao CTN; o que evita o aviso em dobro
        # é o reenvio (que trava a baixa até enviar) e a conferência (que vê a cobrança já PAGA).
        baixas = sessao.scalars(
            select(BaixaCTNRegistro)
            .where(BaixaCTNRegistro.matricula == matricula, BaixaCTNRegistro.aplicada_em.is_(None))
            .order_by(BaixaCTNRegistro.coletado_em, BaixaCTNRegistro.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).all()
        if not baixas:
            continue
        ids = [baixa.id for baixa in baixas]
        totais = replace(resumo)
        try:
            tratamento = _tratar_matricula(sessao, deps, matricula, baixas, agora, resumo)
            sessao.commit()
        except Exception as erro:
            sessao.rollback()
            # Só o tipo e os ids do banco: o texto do erro do SQL leva os parâmetros (a matrícula).
            log.error("as baixas %s do CTN não puderam ser tratadas (%s)", ids, type(erro).__name__)
            for campo in fields(resumo):
                setattr(resumo, campo.name, getattr(totais, campo.name))
            # Pode ter chamado o CTN antes de levantar: conta no teto, senão a matrícula que sempre
            # levanta seria refeita a cada ciclo sem limite. E, passado o prazo, desiste.
            conferidas += 1
            _desistir_das_vencidas(sessao, ids, agora, resumo)
            continue
        if tratamento.conferiu:
            conferidas += 1
        if tratamento.ctn_fora:
            log.warning("o CTN não respondeu à conferência; o resto fica para o próximo ciclo")
            break


def _reenviar_aviso(
    sessao: Session,
    deps: DependenciasDasBaixas,
    baixa: BaixaCTNRegistro,
    cobranca: Cobranca | None,
    conversa: Conversa | None,
    agora: datetime,
) -> None:
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
    elif _em_sombra(deps, cobranca):
        # O serviço voltou à sombra ou o telefone saiu do piloto desde que o aviso foi gravado.
        mensagem.envio = EstadoEnvio.DESCARTADA
        baixa.efeito = QUITOU_SOMBRA
    elif humano_na_conversa(conversa):
        # Um atendente assumiu: o cliente não recebe, e o atendente ganha a nota.
        mensagem.envio = EstadoEnvio.DESCARTADA
        baixa.efeito = QUITOU_COM_HUMANO_NOTA_PENDENTE
    elif not dentro_das_24h(sessao, conversa, agora):
        mensagem.envio = EstadoEnvio.DESCARTADA
        baixa.efeito = QUITOU_FORA_DA_JANELA
    elif _enviar_aviso(sessao, deps, conversa, mensagem):
        baixa.efeito = QUITOU


def _reenviar_nota(
    deps: DependenciasDasBaixas, baixa: BaixaCTNRegistro, conversa: Conversa | None, agora: datetime
) -> None:
    if (
        conversa is None
        or conversa.chatwoot_conversa_id is None
        or agora - baixa.aplicada_em >= PRAZO_DA_CONFERENCIA
    ):
        log.warning("a nota da baixa %s ao atendente não saiu; desiste", baixa.id)
        baixa.efeito = QUITOU_COM_HUMANO_SEM_NOTA
    elif _enviar_nota(deps, conversa):
        baixa.efeito = QUITOU_COM_HUMANO


def _reenviar_um(
    sessao: Session, deps: DependenciasDasBaixas, baixa_id: int, agora: datetime
) -> None:
    # SKIP LOCKED (o SQLite ignora): a baixa fica travada até o envio acabar, então a outra
    # instância que chegar junto a pula em vez de mandar o mesmo aviso. É a única trava segura
    # durante uma chamada ao Chatwoot, e só a desta linha: nada de cobrança nem de conversa.
    baixa = sessao.scalars(
        select(BaixaCTNRegistro)
        .where(BaixaCTNRegistro.id == baixa_id, BaixaCTNRegistro.efeito.in_(_COM_ENVIO_POR_FAZER))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()
    if baixa is None:
        return
    cobranca = sessao.get(Cobranca, baixa.cobranca_id) if baixa.cobranca_id is not None else None
    conversa = _conversa_mais_recente(cobranca) if cobranca is not None else None
    if baixa.efeito == QUITOU_COM_HUMANO_NOTA_PENDENTE:
        _reenviar_nota(deps, baixa, conversa, agora)
    else:
        _reenviar_aviso(sessao, deps, baixa, cobranca, conversa, agora)
    sessao.commit()


def _reenviar_avisos(sessao: Session, deps: DependenciasDasBaixas, agora: datetime) -> None:
    """O aviso gravado PENDENTE que o Chatwoot não recebeu sai agora, se o modo, o piloto e o
    atendente ainda deixam e se as 24h não passaram; senão é descartado. A nota que ficou por enviar
    ao atendente é refeita até o prazo. Uma exceção num item não impede os outros."""
    ids = sessao.scalars(
        select(BaixaCTNRegistro.id)
        .where(BaixaCTNRegistro.efeito.in_(_COM_ENVIO_POR_FAZER))
        .order_by(BaixaCTNRegistro.id)
    ).all()
    for baixa_id in ids:
        try:
            _reenviar_um(sessao, deps, baixa_id, agora)
        except Exception as erro:
            sessao.rollback()
            log.error("o reenvio da baixa %s falhou (%s)", baixa_id, type(erro).__name__)


def ler_baixas(sessao: Session, deps: DependenciasDasBaixas, agora: datetime) -> ResumoBaixas:
    """Uma passada da rotina: reenvia os avisos que ficaram pendentes, lê e grava as baixas novas e
    trata as por tratar. Nunca levanta, e o que loga é só o tipo do erro e ids do banco: a sessão do
    serviço mostra os parâmetros do SQL, e eles levam a matrícula."""
    resumo = ResumoBaixas()
    try:
        _reenviar_avisos(sessao, deps, agora)
    except Exception as erro:
        sessao.rollback()
        log.error("o reenvio dos avisos das baixas falhou (%s)", type(erro).__name__)
    try:
        if _ler_do_ctn(sessao, deps.ctn, agora, resumo):
            _tratar_pendentes(sessao, deps, agora, resumo)
    except Exception as erro:
        sessao.rollback()
        log.error("a rotina das baixas falhou (%s)", type(erro).__name__)
    return resumo
