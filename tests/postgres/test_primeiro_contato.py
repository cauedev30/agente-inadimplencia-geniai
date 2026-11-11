"""A corrida do 1º contato de um testador no Postgres (a 1ª mensagem entregue duas vezes)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from agente.api import chatwoot_webhook as webhook
from agente.api.chatwoot_webhook import EventoMensagem, _conversa_do_1o_contato_de_teste
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.integracoes.ctn_teste import CTNDeCasos
from agente.tipos import StatusCobranca
from tests.conftest import TESTADOR_1
from tests.teste.auxiliares import CHATWOOT_ID
from tests.turno.auxiliares import SEGUNDA_10H


def _evento():
    return EventoMensagem(
        mensagem_id="9001",
        conversa_chatwoot_id=CHATWOOT_ID,
        conta_id=None,
        inbox_id=None,
        tipo="incoming",
        privada=False,
        remetente="contact",
        telefone=TESTADOR_1,
        texto="Oi",
        anexos=[],
    )


def _casos(politica):
    return CTNDeCasos(politica.teste.casos, politica.mensalidade_centavos, SEGUNDA_10H.date)


def _espera_o_bloqueio(motor, pid, prazo=15.0):
    """Espera o backend `pid` ficar preso numa trava (`wait_event_type = 'Lock'`). Cada consulta usa
    uma conexão nova, porque o `pg_stat_activity` não muda dentro de uma transação aberta."""
    limite = time.monotonic() + prazo
    while time.monotonic() < limite:
        with motor.connect() as conexao:
            espera = conexao.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
        if espera == "Lock":
            return
        time.sleep(0.05)
    pytest.fail("a 2ª entrega não ficou presa esperando a 1ª (era para travar no INSERT)")


def test_a_2a_entrega_do_1o_contato_cai_no_indice_unico_e_usa_a_conversa_da_1a(
    motor, fabrica, politica_de_teste, monkeypatch
):
    """Duas conexões criam o caso padrão do mesmo número novo ao mesmo tempo. A 1ª (A) insere o
    cliente e fica sem commit; a 2ª (B) tenta inserir a mesma `matricula_ctn` e o Postgres a deixa
    esperando a 1ª (no SQLite não há conexão concorrente, então isto nunca foi exercitado). Quando A
    faz o commit, B leva `IntegrityError` (violação do índice único de `matricula_ctn`, 23505), o
    savepoint do `_conversa_do_1o_contato_de_teste` desfaz só a tentativa de B e ele devolve a
    conversa que A ligou ao Chatwoot. No fim há um cliente, uma cobrança ATIVA e uma conversa.

    A ordem é determinística: B só é solta depois de A ter inserido, e A só faz o commit depois de o
    `pg_stat_activity` mostrar B presa na trava (nada de `sleep` às cegas)."""
    erros = []
    garantir_original = webhook.garantir_caso

    def garantir_registrando(*args, **kwargs):
        try:
            return garantir_original(*args, **kwargs)
        except IntegrityError as erro:
            erros.append(erro)
            raise

    monkeypatch.setattr(webhook, "garantir_caso", garantir_registrando)
    pid_de_b: list[int] = []
    b_conectada = threading.Event()

    def segunda_entrega() -> int:
        with fabrica() as sessao_b:
            pid_de_b.append(sessao_b.scalar(text("SELECT pg_backend_pid()")))
            b_conectada.set()
            conversa = _conversa_do_1o_contato_de_teste(
                sessao_b, _evento(), _casos(politica_de_teste), politica_de_teste, SEGUNDA_10H
            )
            conversa_id = conversa.id
            sessao_b.commit()
            return conversa_id

    # A sessão fica por dentro do pool: se algo falhar antes do commit, ela fecha (solta a trava) e
    # só então o pool espera a B, que assim não fica presa para sempre.
    with ThreadPoolExecutor(max_workers=1) as pool, fabrica() as sessao_a:
        conversa_de_a = _conversa_do_1o_contato_de_teste(
            sessao_a, _evento(), _casos(politica_de_teste), politica_de_teste, SEGUNDA_10H
        )
        assert conversa_de_a is not None and erros == []  # A inseriu e ainda não fez o commit

        futuro = pool.submit(segunda_entrega)
        assert b_conectada.wait(timeout=15)
        _espera_o_bloqueio(motor, pid_de_b[0])
        sessao_a.commit()
        conversa_de_b_id = futuro.result(timeout=30)

    assert len(erros) == 1  # só a B perdeu
    violacao = erros[0].orig
    assert violacao.sqlstate == "23505"
    assert violacao.diag.table_name == "cliente"
    assert "matricula_ctn" in violacao.diag.constraint_name
    assert conversa_de_b_id == conversa_de_a.id
    with fabrica() as sessao:
        [cliente] = sessao.scalars(select(Cliente)).all()
        assert (cliente.matricula_ctn, cliente.telefone) == ("SIM-1-7777", TESTADOR_1)
        [cobranca] = sessao.scalars(select(Cobranca)).all()
        assert cobranca.status is StatusCobranca.ATIVA
        [conversa] = sessao.scalars(select(Conversa)).all()
        assert conversa.chatwoot_conversa_id == CHATWOOT_ID
        assert sessao.scalar(select(func.count()).select_from(Conversa)) == 1
