"""A conferência no contato (spec do CTN real, seção 4): antes do 1º envio do dia a um cliente,
2 chamadas ao CTN trazem os meses e o valor exatos (os lançamentos e as baixas dele), a propensão
ao cartão (D36) e a lista dos meses. A carteira do retrato diário só tem a contagem de meses.

Regras:
- O CTN cai ou não responde: os valores do retrato ficam, `conferida_em` não é gravado e a
  `Conferencia` volta com `conferida = False`. Nunca levanta para o chamador.
- Quitada (o CTN devolveu rejeitadas, todas foram pagas e elas cobrem os meses do retrato): nada
  é encerrado nem enviado aqui. A
  cobrança guarda `meses_em_aberto = []` e `conferida_em`, os valores do retrato ficam como estão, e
  quem chama (`conferir_antes_do_envio`) recebe `QUITADA`: não cobra. Só sai dessa condição quando
  uma conferência de outro dia achar meses em aberto.
- Nenhuma rejeitada veio (matrícula que o CTN não conhece, ou que não casa com a dos lançamentos)
  contradiz o retrato, que diz que há meses em atraso: NÃO é quitada. `conferida = False`, motivo
  `sem_lancamentos`, sem `conferida_em`.
- Rejeitadas pagas que não cobrem os meses do retrato (o retrato diz 5 e o CTN só tem 3
  lançamentos rejeitados, todos pagos): os meses que sobram não têm mês nem valor conhecidos, então
  NÃO é quitada nem há como abater. `conferida = False`, motivo `rejeitadas_nao_cobrem_o_retrato`,
  sem `conferida_em`; vale como dado suspeito para quem chama.
- O `ult_mesref` do retrato (D55) só DESEMPATA: com as abertas do CTN críveis (sem dado suspeito),
  nada é filtrado. Só quando elas seriam suspeitas (mais meses que o retrato + 1, por exemplo) e a
  cobrança tem `ult_mesref`, vale o conjunto das abertas de mês estritamente posterior a ele, e só
  se forem exatamente os meses do retrato e esse conjunto também for crível. Senão segue suspeito.
  Em 2 de 35 cobranças reais o `ult_mesref` contradizia os lançamentos, por isso ele nunca
  filtra sozinho. Sem `ult_mesref` (CSV, ou cobrança anterior à coluna), nada é filtrado.
- Na mesma rodada de importação a reconferência só ENCOLHE a dívida (D55): a cobrança já conferida
  hoje (`conferida_em` de hoje, no fuso da janela) com `meses_em_aberto` gravado considera só os
  meses abertos que já estavam nesse conjunto. O mês pago sai; pagos todos, o conjunto fica vazio e
  quita; os meses antigos que o CTN mantém abertos (anteriores ao `ult_mesref`) nunca entram, nem um
  mês que apareça no meio do dia (entra na importação seguinte). O teste de dado suspeito usa como
  patamar os meses do retrato (`mensalidades_retrato`), não as mensalidades que a própria
  conferência grava.
- Dado suspeito (mês repetido, valor zero, negativo ou acima do teto sanitário, mais meses abertos
  que o retrato + 1): `conferida = False`, motivo `dado_suspeito`, nada é gravado.
- Quem não conseguiu conferir nunca cita valor estimado: o disparo pula a cobrança e o turno ativo
  espera ou leva ao humano (ver `orquestrador.py`).
- A chamada ao CTN vem antes de qualquer escrita e de qualquer trava de linha: o CTN lento não
  segura a cobrança. Só a gravação curta pega a trava `NO KEY UPDATE`, e o chamador a solta com o
  commit logo em seguida (`conferir_antes_do_envio` faz os dois).
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import httpx
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca
from agente.db.travas import travar_conversas_e_cobranca
from agente.dominio.divida import classificar_regua
from agente.dominio.propensao import eh_propenso
from agente.integracoes.ctn import (
    CTN,
    ClienteNaoEncontrado,
    CTNIndisponivel,
    MensalidadeAberta,
)
from agente.politica import Politica
from agente.tipos import StatusCobranca

log = logging.getLogger(__name__)

# Por que a conferência não valeu mesmo com o CTN no ar (`Conferencia.motivo`).
MOTIVO_SEM_LANCAMENTOS = "sem_lancamentos"
MOTIVO_DADO_SUSPEITO = "dado_suspeito"
MOTIVO_NAO_COBRE_O_RETRATO = "rejeitadas_nao_cobrem_o_retrato"
# O CTN respondeu que não conhece a matrícula: falha daquela pessoa, não do CTN. Para o turno e o
# disparo vale como qualquer conferência que não rodou (`NAO_CONFERIDA`); a rotina das baixas a
# encerra na hora em vez de esperar o CTN voltar.
MOTIVO_CLIENTE_NAO_ENCONTRADO = "cliente_nao_encontrado"

# Uma mensalidade acima disto é dado errado: as reais vão de R$ 10 a R$ 99 (o
# `politica.mensalidade_centavos` fixo é só uma estimativa).
TETO_SANITARIO_MENSALIDADE_CENTAVOS = 100_000


class ResultadoConferencia(StrEnum):
    """O que `conferir_antes_do_envio` diz a quem vai enviar."""

    CONFERIDA = "conferida"
    QUITADA = "quitada"
    SEM_LANCAMENTOS = "sem_lancamentos"
    DADO_SUSPEITO = "dado_suspeito"
    # O CTN fora do ar, a cobrança que fechou durante a chamada ou qualquer outra falha: os valores
    # são os do retrato e não podem ser citados.
    NAO_CONFERIDA = "nao_conferida"


@dataclass(frozen=True)
class Conferencia:
    """O resultado de uma conferência. Sem `conferida`, os valores são os do retrato e `motivo`
    diz por que (`None` quando o CTN falhou ou a cobrança deixou de estar ativa; o
    `cliente_nao_encontrado` é a falha daquela matrícula, não do CTN)."""

    meses_em_aberto: list[str] = field(default_factory=list)
    divida_centavos: int = 0
    propenso: bool = False
    conferida: bool = False
    motivo: str | None = None

    @property
    def quitada_no_ctn(self) -> bool:
        return self.conferida and not self.meses_em_aberto


def _dia(momento: datetime, politica: Politica):
    return momento.astimezone(politica.janela.zona).date()


def precisa_conferir(cobranca: Cobranca, politica: Politica, agora: datetime) -> bool:
    """Nunca conferida, ou conferida em outro dia (no fuso da janela)."""
    if cobranca.conferida_em is None:
        return True
    return _dia(cobranca.conferida_em, politica) != _dia(agora, politica)


def _do_retrato(cobranca: Cobranca, motivo: str | None = None) -> Conferencia:
    return Conferencia(
        meses_em_aberto=list(cobranca.meses_em_aberto or []),
        divida_centavos=cobranca.divida_centavos,
        propenso=cobranca.propenso_cartao,
        conferida=False,
        motivo=motivo,
    )


def _dado_suspeito(abertas: list[MensalidadeAberta], meses_do_retrato: int) -> bool:
    """O dinheiro que o CTN trouxe não é crível: não vai ao cliente."""
    meses = [mensalidade.mes_ano for mensalidade in abertas]
    if len(set(meses)) != len(meses):
        return True
    if any(
        not 0 < mensalidade.valor_centavos <= TETO_SANITARIO_MENSALIDADE_CENTAVOS
        for mensalidade in abertas
    ):
        return True
    # O retrato e os lançamentos vêm do mesmo CTN: mais de um mês de diferença é dado errado.
    if len(abertas) > meses_do_retrato + 1:
        return True
    return sum(mensalidade.valor_centavos for mensalidade in abertas) <= 0


def _mes_do(ult_mesref: date) -> str:
    return f"{ult_mesref.year:04d}/{ult_mesref.month:02d}"


def _meses_do_retrato(cobranca: Cobranca) -> int:
    """Os meses em atraso que o retrato da importação trouxe; sem eles (cobrança anterior à
    coluna), os da cobrança."""
    retrato = cobranca.mensalidades_retrato
    return cobranca.mensalidades_abertas if retrato is None else retrato


def _conhecidas_hoje(cobranca: Cobranca, politica: Politica, agora: datetime) -> set[str] | None:
    """Os meses em aberto que a conferência de hoje gravou, ou `None` se a cobrança não foi
    conferida hoje (ou nunca teve a lista). Lista vazia é um conjunto vazio (já quitada hoje)."""
    if cobranca.meses_em_aberto is None or precisa_conferir(cobranca, politica, agora):
        return None
    return set(cobranca.meses_em_aberto)


def _abertas_a_cobrar(
    cobranca: Cobranca, abertas: list[MensalidadeAberta], politica: Politica, agora: datetime
) -> tuple[list[MensalidadeAberta], bool]:
    """As abertas que contam como dívida e se o `ult_mesref` as escolheu (D55).

    Conferida hoje com a lista de meses gravada: só os meses dessa lista (a dívida só encolhe; os
    antigos que o CTN mantém abertos e o mês novo do meio do dia ficam de fora). Senão, crível,
    devolve todas. Suspeitas e com `ult_mesref`, devolve só as de mês posterior a ele, mas apenas
    se forem exatamente os meses do retrato e críveis; senão devolve todas (e seguem suspeitas)."""
    conhecidas = _conhecidas_hoje(cobranca, politica, agora)
    if conhecidas is not None:
        return [m for m in abertas if m.mes_ano in conhecidas], False
    retrato = _meses_do_retrato(cobranca)
    if not abertas or not _dado_suspeito(abertas, retrato):
        return abertas, False
    if cobranca.ult_mesref is None:
        return abertas, False
    corte = _mes_do(cobranca.ult_mesref)
    depois = [m for m in abertas if m.mes_ano > corte]
    if len(depois) == retrato and not _dado_suspeito(depois, retrato):
        return depois, True
    return abertas, False


def conferir_cobranca(
    sessao: Session,
    ctn: CTN,
    politica: Politica,
    cobranca_id: int,
    agora: datetime,
    *,
    travar_conversas: bool = False,
) -> Conferencia:
    """Confere a cobrança no CTN e grava o que leu. Faz `flush`, não `commit`.

    `travar_conversas` é para quem vai mexer na conversa logo depois (a rotina das baixas
    externas): trava as conversas e depois a cobrança, na ordem de `db/travas.py`, em vez de só a
    cobrança. A chamada ao CTN continua vindo antes de qualquer trava."""
    cobranca = sessao.get_one(Cobranca, cobranca_id)
    matricula = cobranca.cliente.matricula_ctn
    try:
        consulta = ctn.consultar_mensalidades(matricula)
        abertas, desempatou = _abertas_a_cobrar(cobranca, consulta.abertas, politica, agora)
        # Um mês a mais, porque o mês corrente pode não ter pagamento ainda (como no importar).
        historico = (
            ctn.historico_pagamentos(matricula, meses=politica.propensao.n_meses + 1)
            if abertas
            else []
        )
    except ClienteNaoEncontrado:
        log.warning(
            "o CTN não conhece a matrícula da cobrança %s; segue com o retrato", cobranca_id
        )
        return _do_retrato(cobranca, MOTIVO_CLIENTE_NAO_ENCONTRADO)
    except (CTNIndisponivel, httpx.HTTPError):
        log.warning("a conferência da cobrança %s falhou; segue com o retrato", cobranca_id)
        return _do_retrato(cobranca)

    # Só aqui, com tudo lido, a trava curta da linha: a cobrança pode ter fechado durante a chamada.
    if travar_conversas:
        travar_conversas_e_cobranca(sessao, cobranca_id)
    else:
        sessao.refresh(cobranca, with_for_update={"key_share": True})
    if cobranca.status is not StatusCobranca.ATIVA:
        return _do_retrato(cobranca)

    if not abertas and consulta.rejeitadas_lidas == 0:
        # O retrato diz que há meses em atraso e o CTN não tem lançamento nenhum: contradição, não
        # "quitada" (o `lancamentos` não muda no mesmo dia). Nada é gravado.
        log.warning("a cobrança %s não tem lançamentos no CTN; segue sem conferir", cobranca_id)
        return _do_retrato(cobranca, MOTIVO_SEM_LANCAMENTOS)
    if not abertas and consulta.rejeitadas_lidas < _meses_do_retrato(cobranca):
        # Todas as rejeitadas estão pagas, mas o retrato diz que há mais meses em atraso do que
        # elas: os que sobram não têm mês nem valor, então não dá para quitar nem para abater.
        log.warning(
            "as rejeitadas da cobrança %s não cobrem os meses do retrato; segue sem conferir",
            cobranca_id,
        )
        return _do_retrato(cobranca, MOTIVO_NAO_COBRE_O_RETRATO)
    if abertas and not desempatou and _dado_suspeito(abertas, _meses_do_retrato(cobranca)):
        log.warning("a conferência da cobrança %s trouxe dado suspeito; descartada", cobranca_id)
        return _do_retrato(cobranca, MOTIVO_DADO_SUSPEITO)

    cobranca.conferida_em = agora
    meses = sorted(mensalidade.mes_ano for mensalidade in abertas)
    cobranca.meses_em_aberto = meses
    if not abertas:
        # Havia rejeitadas e todas foram pagas.
        sessao.flush()
        return Conferencia(
            meses_em_aberto=[], divida_centavos=cobranca.divida_centavos, conferida=True
        )

    anterior = cobranca.mensalidades_abertas
    if len(abertas) != anterior and cobranca.regua == classificar_regua(anterior, None, politica):
        # A régua que veio da contagem de meses acompanha a contagem nova; a informada pela lista
        # (NR, Prévia, G5) fica como está.
        cobranca.regua = classificar_regua(len(abertas), None, politica)
    cobranca.mensalidades_abertas = len(abertas)
    cobranca.divida_centavos = sum(mensalidade.valor_centavos for mensalidade in abertas)
    cobranca.propenso_cartao = eh_propenso(historico, politica)
    sessao.flush()
    return Conferencia(
        meses_em_aberto=meses,
        divida_centavos=cobranca.divida_centavos,
        propenso=cobranca.propenso_cartao,
        conferida=True,
    )


def conferir_antes_do_envio(
    sessao: Session, ctn: CTN, politica: Politica, cobranca_id: int, agora: datetime
) -> ResultadoConferencia:
    """Confere, uma vez por dia, antes do 1º envio, e diz o que o chamador pode fazer.

    Chamar com a sessão sem escritas pendentes e sem trava: o commit no fim solta a trava curta da
    gravação e vale como o ponto de partida do que o chamador faz depois. Só `CONFERIDA` deixa
    citar o valor da cobrança; `QUITADA` (também no mesmo dia, sem nova chamada ao CTN) não cobra;
    as demais (`SEM_LANCAMENTOS`, `DADO_SUSPEITO`, `NAO_CONFERIDA`) não têm conferência de hoje, e o
    valor do retrato é só uma estimativa.
    """
    cobranca = sessao.get_one(Cobranca, cobranca_id)
    if not precisa_conferir(cobranca, politica, agora):
        return (
            ResultadoConferencia.QUITADA
            if quitada_hoje(cobranca, politica, agora)
            else ResultadoConferencia.CONFERIDA
        )
    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca_id, agora)
    sessao.commit()
    if conferencia.quitada_no_ctn:
        return ResultadoConferencia.QUITADA
    if conferencia.conferida:
        return ResultadoConferencia.CONFERIDA
    return {
        MOTIVO_SEM_LANCAMENTOS: ResultadoConferencia.SEM_LANCAMENTOS,
        MOTIVO_DADO_SUSPEITO: ResultadoConferencia.DADO_SUSPEITO,
        MOTIVO_NAO_COBRE_O_RETRATO: ResultadoConferencia.DADO_SUSPEITO,
    }.get(conferencia.motivo, ResultadoConferencia.NAO_CONFERIDA)


def quitada_hoje(cobranca: Cobranca, politica: Politica, agora: datetime) -> bool:
    """A conferência de hoje achou a cobrança sem meses em aberto."""
    return (
        cobranca.conferida_em is not None
        and not precisa_conferir(cobranca, politica, agora)
        and cobranca.meses_em_aberto == []
    )
