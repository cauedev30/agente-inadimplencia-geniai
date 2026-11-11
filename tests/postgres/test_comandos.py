"""Os comandos do modo teste (`/caso`, `/paguei`, `/paguei n` e `/nota`) no Postgres migrado.

O desenho e os auxiliares são os de `tests/teste/` (SQLite em memória); aqui o banco é o Postgres, e
cada teste confere o que só o Postgres pode mostrar: os dados lidos de volta do banco (`expire_all`
antes dos asserts), o JSON dos meses e das pendências, as datas com fuso, as travas
`FOR UPDATE` e o índice parcial de uma cobrança ATIVA por cliente."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from agente.db.modelos import (
    BaixaCTNRegistro,
    Cliente,
    Cobranca,
    Mensagem,
    NotaTeste,
)
from agente.pagamento import confirmar
from agente.pagamento.baixas_ctn import ABATEU, CONFERIDA_JUNTO
from agente.pagamento.concluir import concluir_pagamentos
from agente.teste.notas import relatorio_das_notas
from agente.tipos import (
    EstadoConversa,
    OrigemMensagem,
    Pendencia,
    ResultadoTurno,
    StatusCobranca,
    StatusPagamento,
)
from tests.teste.auxiliares import CHATWOOT_ID
from tests.teste.test_paguei import _conversa_do_chatwoot, _no_caso, _pagamento, _ultimo_turno
from tests.turno.auxiliares import SEGUNDA_10H


@pytest.fixture(autouse=True)
def _relogio_da_confirmacao(monkeypatch):
    """A `confirmar_pagamento` agenda e grava no relógio dela: o do turno, nos testes."""
    monkeypatch.setattr(confirmar, "_agora", lambda: SEGUNDA_10H + timedelta(minutes=5))


@pytest.fixture
def depois_do_1o_contato(sessao, testador, deps):
    """O caso 1, depois do 1º contato (a abertura saiu e o cliente já falou)."""
    testador.manda("Oi")
    testador.roda(deps)
    return testador


@pytest.fixture
def conversa_aberta(sessao, testador, deps):
    """O caso 1, depois do 1º contato e de uma conversa de verdade (o Luna respondeu)."""
    testador.manda("Oi")
    testador.roda(deps)
    testador.manda("Quanto eu devo?")
    testador.roda(deps)
    return testador


def test_caso_n_troca_o_caso_e_cria_cliente_e_cobranca_no_postgres(
    sessao, testador, conversa_de_teste, deps
):
    antiga = conversa_de_teste.cobranca
    testador.manda("/caso 3")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.expire_all()  # tudo abaixo é lido de volta do Postgres
    assert antiga.status is StatusCobranca.ENCERRADA
    assert antiga.encerrada_motivo == "troca_de_caso"
    assert antiga.encerrada_em == SEGUNDA_10H + timedelta(minutes=testador.n)
    nova_conversa = _conversa_do_chatwoot(sessao)
    cobranca = nova_conversa.cobranca
    assert cobranca.cliente.matricula_ctn == "SIM-3-7777"
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.mensalidades_abertas == 4
    assert isinstance(cobranca.meses_em_aberto, list) and len(cobranca.meses_em_aberto) == 4
    assert all(len(mes) == 7 and mes[4] == "/" for mes in cobranca.meses_em_aberto)  # AAAA/MM
    assert cobranca.conferida_em.tzinfo is not None
    assert cobranca.propenso_cartao is False
    assert sessao.scalar(select(func.count()).select_from(Cliente)) == 2
    ativas = sessao.scalars(select(Cobranca).where(Cobranca.status == StatusCobranca.ATIVA)).all()
    assert [c.id for c in ativas] == [cobranca.id]
    assert _ultimo_turno(sessao).acao_principal == "TESTE:CASO"
    assert deps.llm_turno.pedidos == []


def test_paguei_baixa_o_pix_pendente_e_a_rotina_conclui_no_postgres(
    sessao, depois_do_1o_contato, conversa_de_teste, deps
):
    cobranca = conversa_de_teste.cobranca
    pix = _pagamento(sessao, cobranca)
    depois_do_1o_contato.manda("/paguei")

    assert depois_do_1o_contato.roda(deps) is ResultadoTurno.FIXA

    sessao.expire_all()
    assert pix.status is StatusPagamento.PAGO
    assert cobranca.status is StatusCobranca.PAGA
    assert conversa_de_teste.estado is EstadoConversa.ENCERRADO
    assert Pendencia.MENSAGEM.value in pix.pendencias
    assert Pendencia.BAIXA.value in pix.pendencias
    assert _ultimo_turno(sessao).acao_principal == "TESTE:PAGUEI"
    # A rotina das pendências (SELECT ... FOR UPDATE SKIP LOCKED) manda a confirmação e dá a baixa.
    assert concluir_pagamentos(sessao, deps, SEGUNDA_10H + timedelta(minutes=6)) == 1
    sessao.expire_all()
    assert pix.pendencias == [] and pix.baixa_ctn_em is not None
    assert "R$ 100,20" in deps.chatwoot.enviadas(CHATWOOT_ID)[-1]


def test_paguei_n_abate_os_meses_e_a_regua_cai_no_postgres(
    sessao, testador, conversa_de_teste, deps
):
    nova = _no_caso(sessao, testador, deps, 3)
    cobranca = _conversa_do_chatwoot(sessao).cobranca
    assert cobranca.mensalidades_abertas == 4
    nova.manda("/paguei 2")

    assert nova.roda(deps) is ResultadoTurno.FIXA

    sessao.expire_all()
    assert cobranca.status is StatusCobranca.ATIVA
    assert (cobranca.mensalidades_abertas, cobranca.divida_centavos) == (2, 6680)
    assert cobranca.regua.value == "R2"
    baixas = list(sessao.scalars(select(BaixaCTNRegistro).order_by(BaixaCTNRegistro.id)))
    assert [b.efeito for b in baixas] == [ABATEU, CONFERIDA_JUNTO]
    assert all(b.cobranca_id == cobranca.id for b in baixas)
    assert _ultimo_turno(sessao).acao_principal == "TESTE:PAGUEI"


def test_nota_grava_presa_ao_turno_e_o_relatorio_filtra_pela_data_no_postgres(
    sessao, conversa_aberta, conversa_de_teste, deps, politica_de_teste
):
    turno_do_llm = _ultimo_turno(sessao)
    conversa_aberta.manda("/nota a resposta saiu seca demais")

    assert conversa_aberta.roda(deps) is ResultadoTurno.FIXA

    sessao.expire_all()
    [nota] = sessao.scalars(select(NotaTeste)).all()
    assert nota.turno_id == turno_do_llm.id
    assert nota.conversa_id == conversa_de_teste.id
    assert nota.caso == 1
    assert nota.texto == "a resposta saiu seca demais"
    comando = sessao.get(Mensagem, nota.mensagem_id)
    assert (comando.origem, comando.conta_no_teto) == (OrigemMensagem.TESTE, False)
    assert nota.criada_em.tzinfo is not None
    assert "1 nota(s)" in relatorio_das_notas(sessao, politica_de_teste, date(2026, 10, 26))
    assert "Nenhuma nota" in relatorio_das_notas(sessao, politica_de_teste, date(2026, 10, 27))
