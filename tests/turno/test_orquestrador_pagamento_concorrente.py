"""O pagamento confirmado durante o turno: o turno relê antes de gravar e não sobrescreve nada.

O webhook roda em outra transação. O SQLite não trava linha, então a corrida é simulada na ordem dos
fatos: um UPDATE por fora da sessão, que deixa a conversa e a cobrança da sessão do turno velhas, no
meio da chamada ao Luna.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cobranca, Conversa, Mensagem, Pagamento, Transicao, Turno
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.tipos import (
    EstadoConversa,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.orquestrador import processar_turno

from .auxiliares import _bruto, _deps
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
class LLMQueEspera(LLMTurnoFalso):
    """Enquanto o Luna responde, o pagamento chega."""

    ao_chamar: object = None

    def completar_json(self, sistema, usuario):
        if self.ao_chamar is not None:
            self.ao_chamar()
            self.ao_chamar = None
        return super().completar_json(sistema, usuario)


def _pergunta(sessao, conversa, texto="quanto eu devo?"):
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, texto, "cw-1", False, SEGUNDA_10H
    )


def _estado_no_banco(sessao, conversa):
    sessao.expire_all()
    return sessao.get_one(Conversa, conversa.id).estado, sessao.get_one(
        Cobranca, conversa.cobranca_id
    ).status


def test_o_pagamento_no_meio_do_luna_nao_gera_envio_nem_sobrescreve_o_encerrado(
    sessao, ligada, politica
):
    llm = LLMQueEspera(respostas=[_bruto()], registrar=True)
    llm.ao_chamar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, llm=llm)
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


def test_o_pagamento_no_meio_do_luna_nao_manda_o_canal_externo_depois_da_confirmacao(
    sessao, ligada, politica
):
    # O Luna lê "já paguei": o turno sairia para o humano (CONCILIACAO), mas o pagamento chegou.
    saida = _bruto(alega_pagamento=True, acao="NONE", reply="")
    llm = LLMQueEspera(respostas=[saida], registrar=True)
    llm.ao_chamar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, llm=llm)
    _pergunta(sessao, ligada, "já paguei")

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert deps.chatwoot.chamadas == []
    assert _estado_no_banco(sessao, ligada) == (EstadoConversa.ENCERRADO, StatusCobranca.PAGA)
    assert sessao.get_one(Conversa, ligada.id).motivo_humano is None


def test_sem_pagamento_no_meio_o_turno_segue_igual(sessao, ligada, politica):
    deps = _deps(politica, [_bruto()])
    _pergunta(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _estado_no_banco(sessao, ligada) == (
        EstadoConversa.AGUARDANDO_PAGAMENTO,
        StatusCobranca.ATIVA,
    )


def test_o_turno_trava_a_conversa_antes_da_cobranca_como_a_confirmacao(sessao, ligada, politica):
    deps = _deps(politica, [_bruto()])
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


def test_a_trava_da_cobranca_vem_antes_do_savepoint_da_acao(sessao, ligada, politica, monkeypatch):
    # O savepoint da ação pode voltar atrás: uma trava tomada dentro dele seria solta.
    deps = _deps(politica)
    _pergunta(sessao, ligada, "Pagar com Pix")  # sem o Luna: nenhuma releitura antes do botão
    chamadas = []
    refresh, begin_nested = sessao.refresh, sessao.begin_nested
    monkeypatch.setattr(
        sessao, "refresh", lambda *a, **k: (chamadas.append("refresh"), refresh(*a, **k))[1]
    )
    monkeypatch.setattr(
        sessao,
        "begin_nested",
        lambda *a, **k: (chamadas.append("savepoint"), begin_nested(*a, **k))[1],
    )

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # O 1º savepoint é o do turno; o 2º, o da ação, precedido da releitura com trava.
    posicoes = [i for i, nome in enumerate(chamadas) if nome == "savepoint"]
    assert chamadas[posicoes[1] - 1] == "refresh"
