"""A conferência no contato (spec do CTN real, seção 4): antes do 1º envio do dia a um cliente,
2 chamadas ao CTN trazem os meses e o valor exatos (os lançamentos e as baixas dele), a propensão
ao cartão (D36) e a lista dos meses. A carteira do retrato diário só tem a contagem de meses.

Regras:
- O CTN cai ou não responde: os valores do retrato ficam, `conferida_em` não é gravado e a
  `Conferencia` volta com `conferida = False`. Nunca levanta para o chamador.
- Zero meses em aberto (o cliente já pagou, ou a matrícula não casa com a dos lançamentos): nada é
  encerrado nem enviado aqui. A cobrança guarda `meses_em_aberto = []` e `conferida_em`, os valores
  do retrato ficam como estão, e quem chama (`conferir_antes_do_envio`) recebe `False`: não cobra.
  A cobrança só sai dessa condição quando uma conferência de outro dia achar meses em aberto.
- A chamada ao CTN vem antes de qualquer escrita e de qualquer trava de linha: o CTN lento não
  segura a cobrança. Só a gravação curta pega a trava `NO KEY UPDATE`, e o chamador a solta com o
  commit logo em seguida (`conferir_antes_do_envio` faz os dois).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca
from agente.dominio.divida import classificar_regua
from agente.dominio.propensao import eh_propenso
from agente.integracoes.ctn import CTN, ClienteNaoEncontrado, CTNIndisponivel
from agente.politica import Politica
from agente.tipos import StatusCobranca

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Conferencia:
    """O resultado de uma conferência. Sem `conferida`, os valores são os do retrato."""

    meses_em_aberto: list[str] = field(default_factory=list)
    divida_centavos: int = 0
    propenso: bool = False
    conferida: bool = False

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


def _do_retrato(cobranca: Cobranca) -> Conferencia:
    return Conferencia(
        meses_em_aberto=list(cobranca.meses_em_aberto or []),
        divida_centavos=cobranca.divida_centavos,
        propenso=cobranca.propenso_cartao,
        conferida=False,
    )


def conferir_cobranca(
    sessao: Session, ctn: CTN, politica: Politica, cobranca_id: int, agora: datetime
) -> Conferencia:
    """Confere a cobrança no CTN e grava o que leu. Faz `flush`, não `commit`."""
    cobranca = sessao.get_one(Cobranca, cobranca_id)
    matricula = cobranca.cliente.matricula_ctn
    try:
        abertas = ctn.mensalidades_em_aberto(matricula)
        # Um mês a mais, porque o mês corrente pode não ter pagamento ainda (como no importar).
        historico = (
            ctn.historico_pagamentos(matricula, meses=politica.propensao.n_meses + 1)
            if abertas
            else []
        )
    except (CTNIndisponivel, ClienteNaoEncontrado, httpx.HTTPError):
        log.warning("a conferência da cobrança %s falhou; segue com o retrato", cobranca_id)
        return _do_retrato(cobranca)

    # Só aqui, com tudo lido, a trava curta da linha: a cobrança pode ter fechado durante a chamada.
    sessao.refresh(cobranca, with_for_update={"key_share": True})
    if cobranca.status is not StatusCobranca.ATIVA:
        return _do_retrato(cobranca)

    cobranca.conferida_em = agora
    meses = sorted(mensalidade.mes_ano for mensalidade in abertas)
    cobranca.meses_em_aberto = meses
    if not abertas:
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
) -> bool:
    """Confere, uma vez por dia, antes do 1º envio. Devolve `False` se a cobrança não deve sair.

    Chamar com a sessão sem escritas pendentes e sem trava: o commit no fim solta a trava curta da
    gravação e vale como o ponto de partida do que o chamador faz depois. O CTN fora do ar não
    bloqueia (devolve `True`, com o retrato). A cobrança que a conferência de hoje achou sem meses
    em aberto devolve `False`, no mesmo dia e sem nova chamada ao CTN.
    """
    cobranca = sessao.get_one(Cobranca, cobranca_id)
    if precisa_conferir(cobranca, politica, agora):
        conferir_cobranca(sessao, ctn, politica, cobranca_id, agora)
        sessao.commit()
    return not quitada_hoje(cobranca, politica, agora)


def quitada_hoje(cobranca: Cobranca, politica: Politica, agora: datetime) -> bool:
    """A conferência de hoje achou a cobrança sem meses em aberto."""
    return (
        cobranca.conferida_em is not None
        and not precisa_conferir(cobranca, politica, agora)
        and cobranca.meses_em_aberto == []
    )
