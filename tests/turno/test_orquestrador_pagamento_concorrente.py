"""O pagamento confirmado durante o turno: o turno relê antes de gravar e não sobrescreve nada.

O webhook roda em outra transação. O SQLite não trava linha, então a corrida é simulada na ordem dos
fatos: um UPDATE por fora da sessão, que deixa a conversa e a cobrança da sessão do turno velhas, no
meio do Jev ou do LLM.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cobranca, Conversa, Mensagem, Pagamento, Transicao, Turno
from agente.integracoes.jev import responder
from agente.integracoes.redator import RedatorFalso
from agente.tipos import (
    EstadoConversa,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.orquestrador import processar_turno

from .test_orquestrador_ativo import _deps
from .test_orquestrador_gates import SEGUNDA_10H


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _pagar_por_fora(sessao, conversa):
    """O que o webhook de pagamento commita em outra transação: a sessão do turno não vê."""
    sessao.execute(
        update(Cobranca)
        .where(Cobranca.id == conversa.cobranca_id)
        .values(status=StatusCobranca.PAGA)
        .execution_options(synchronize_session=False)
    )
    sessao.execute(
        update(Conversa)
        .where(Conversa.id == conversa.id)
        .values(estado=EstadoConversa.ENCERRADO)
        .execution_options(synchronize_session=False)
    )


@dataclass
class RedatorQueEspera(RedatorFalso):
    """Enquanto o LLM redige, o pagamento chega."""

    ao_redigir: object = None

    def redigir(self, pedido):
        if self.ao_redigir is not None:
            self.ao_redigir()
            self.ao_redigir = None
        return super().redigir(pedido)


def _pergunta(sessao, conversa, texto="quanto eu devo?"):
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, texto, "cw-1", False, SEGUNDA_10H
    )


def _estado_no_banco(sessao, conversa):
    sessao.expire_all()
    return sessao.get_one(Conversa, conversa.id).estado, sessao.get_one(
        Cobranca, conversa.cobranca_id
    ).status


def test_o_pagamento_no_meio_do_llm_nao_gera_envio_nem_sobrescreve_o_encerrado(
    sessao, ligada, politica
):
    redator = RedatorQueEspera(registrar=True)
    redator.ao_redigir = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, redator=redator)
    _pergunta(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert deps.chatwoot.chamadas == []
    assert sessao.scalars(select(Pagamento)).all() == []  # o Pix do turno voltou atrás
    outras = select(Mensagem).where(Mensagem.origem != OrigemMensagem.CLIENTE)
    assert sessao.scalars(outras).all() == []
    # O turno não escreveu estado nenhum: nem o AGUARDANDO_PAGAMENTO, nem o humano. (O UPDATE
    # simulado do webhook fica dentro do savepoint do turno, que volta atrás; no Postgres ele é
    # outra transação e o ENCERRADO fica.)
    assert sessao.scalars(select(Transicao)).all() == []
    turno = sessao.scalars(select(Turno)).one()
    assert turno.resultado is ResultadoTurno.IGNORADO and turno.texto_enviado is None


def test_o_pagamento_no_meio_do_jev_nao_manda_o_canal_externo_depois_da_confirmacao(
    sessao, ligada, politica
):
    chegou = []

    def pagamento_chega(pergunta, estado):
        if not chegou:
            chegou.append(True)
            _pagar_por_fora(sessao, ligada)

    # O Jev 1 lê "já paguei": o turno sairia para o humano (CONCILIACAO), mas o pagamento chegou.
    deps = _deps(
        politica,
        regras=[pagamento_chega, responder("alega_pagamento", True, 0.95, 0.95)],
    )
    _pergunta(sessao, ligada, "já paguei")

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert deps.chatwoot.chamadas == []
    assert _estado_no_banco(sessao, ligada) == (EstadoConversa.ENCERRADO, StatusCobranca.PAGA)
    assert sessao.get_one(Conversa, ligada.id).motivo_humano is None


def test_sem_pagamento_no_meio_o_turno_segue_igual(sessao, ligada, politica):
    deps = _deps(politica)
    _pergunta(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _estado_no_banco(sessao, ligada) == (
        EstadoConversa.AGUARDANDO_PAGAMENTO,
        StatusCobranca.ATIVA,
    )


def test_o_turno_trava_a_conversa_antes_da_cobranca_como_a_confirmacao(sessao, ligada, politica):
    deps = _deps(politica)
    _pergunta(sessao, ligada)
    travas = []

    def capturar(estado):
        sql = estado.statement.compile(dialect=postgresql.dialect()).string
        if sql.rstrip().endswith("FOR NO KEY UPDATE"):
            travas.append(sql.split("FROM ")[1].split()[0])

    event.listen(sessao, "do_orm_execute", capturar)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert travas and travas[0] == "conversa"
    assert travas.index("conversa") < travas.index("cobranca")
