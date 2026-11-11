"""Um erro de código no turno também tem teto: tentativas, um só handoff e a saída da fila.

Diferente da falha de integração, o erro interno é um bug: o turno é desfeito e volta para a fila
com a espera que dobra; na última tentativa a conversa vai ao humano com `ERRO_INTERNO`, e o stack
trace fica no log. Em sombra, o turno é registrado com o erro e sai da fila, sem handoff.
"""

import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Turno
from agente.tipos import (
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno import orquestrador
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.orquestrador import processar_turno

from .auxiliares import SEGUNDA_10H, _bruto, _deps
from .test_orquestrador_sombra import _deps as _deps_sombra

RESPOSTAS = [_bruto()] * 5


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


class Bug:
    """A função do turno com defeito: levanta `RuntimeError` nas primeiras `vezes` chamadas."""

    def __init__(self, original, vezes=10**6):
        self.original, self.vezes, self.chamadas = original, vezes, 0

    def __call__(self, *args, **kwargs):
        self.chamadas += 1
        if self.chamadas <= self.vezes:
            raise RuntimeError("bug no turno")
        return self.original(*args, **kwargs)


@pytest.fixture
def bug(monkeypatch):
    def instalar(vezes=10**6):
        defeito = Bug(orquestrador._seguir, vezes)
        monkeypatch.setattr(orquestrador, "_seguir", defeito)
        return defeito

    return instalar


def _pergunta(sessao, conversa):
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, "quanto eu devo?", "cw-1", False, SEGUNDA_10H
    )
    sessao.commit()


def _handoffs(deps):
    return [c for c in deps.chatwoot.chamadas if c[0] == "mudar_status"]


def _rodar_ate_o_fim(sessao, conversa, deps, vezes):
    """Roda o turno nos horários que a própria conversa agendou; devolve os resultados."""
    resultados, agora = [], SEGUNDA_10H
    for _ in range(vezes):
        resultados.append(processar_turno(sessao, conversa.id, agora, deps))
        sessao.commit()
        if conversa.turno_em is None:
            break
        agora = conversa.turno_em
    return resultados


def test_o_erro_de_codigo_volta_para_a_fila_e_na_ultima_tentativa_vai_ao_humano(
    sessao, ligada, politica, bug
):
    defeito = bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)
    n = politica.erro_interno_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados == [ResultadoTurno.ERRO] * (n - 1) + [ResultadoTurno.HANDOFF]
    assert defeito.chamadas == n
    assert len(_handoffs(deps)) == 1
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.ERRO_INTERNO,
    )
    assert (ligada.turno_em, ligada.erros_turno, ligada.falhas_turno) == (None, 0, 0)


def test_so_a_mensagem_fixa_do_handoff_sai_ao_cliente(sessao, ligada, politica, bug):
    bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)
    n = politica.erro_interno_tentativas

    agora = SEGUNDA_10H
    for _ in range(n - 1):
        assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.ERRO
        sessao.commit()
        agora = ligada.turno_em
    assert deps.chatwoot.enviadas(555) == []

    resultado = processar_turno(sessao, ligada.id, agora, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def test_cada_erro_fica_no_historico_do_turno_e_nao_consome_a_mensagem(
    sessao, ligada, politica, bug
):
    bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    erro = sessao.scalars(select(Turno)).one()
    assert erro.resultado is ResultadoTurno.ERRO
    assert erro.modo is Modo.ATIVO
    assert erro.redacao == {"erro": "RuntimeError", "tentativa": 1}
    assert ligada.ultima_tomada_id is None
    assert ligada.erros_turno == 1 and ligada.falhas_turno == 0
    assert ligada.turno_em == SEGUNDA_10H + timedelta(seconds=politica.integracao_espera_segundos)


def test_a_espera_entre_as_tentativas_dobra(sessao, ligada, politica, bug):
    bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)
    base = politica.integracao_espera_segundos

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    sessao.commit()
    segunda = ligada.turno_em
    processar_turno(sessao, ligada.id, segunda, deps)

    assert ligada.turno_em == segunda + timedelta(seconds=2 * base)
    assert ligada.erros_turno == 2


def test_o_stack_trace_do_erro_fica_no_log(sessao, ligada, politica, bug, caplog):
    bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)

    with caplog.at_level(logging.ERROR, logger=orquestrador.log.name):
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    [registro] = [r for r in caplog.records if r.exc_info]
    assert registro.exc_info[0] is RuntimeError
    assert str(ligada.id) in registro.getMessage()


def test_o_bug_que_passa_sozinho_fecha_o_turno_e_zera_a_conta(sessao, ligada, politica, bug):
    bug(vezes=1)
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    assert ligada.erros_turno == 1

    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert ligada.erros_turno == 0
    assert _handoffs(deps) == []


def test_a_falha_de_integracao_nao_conta_como_erro_interno(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)

    def pix_fora(*args, **kwargs):
        raise httpx.ConnectError("o banco não respondeu")

    deps.pix.criar_cobranca = pix_fora
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    assert (ligada.falhas_turno, ligada.erros_turno) == (1, 0)


def test_o_handoff_que_tambem_quebra_tira_a_conversa_da_fila_sem_laco(
    sessao, ligada, politica, bug, caplog
):
    bug()
    deps = _deps(politica, RESPOSTAS)

    def nota_privada(conversa_id, texto):
        raise KeyError("bug no handoff")

    deps.chatwoot.nota_privada = nota_privada
    _pergunta(sessao, ligada)
    n = politica.erro_interno_tentativas

    with caplog.at_level(logging.ERROR, logger=orquestrador.log.name):
        resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 3)

    assert resultados == [ResultadoTurno.ERRO] * n
    assert ligada.turno_em is None
    assert ligada.erros_turno == n
    assert _handoffs(deps) == [] and deps.chatwoot.enviadas(555) == []
    assert "sai da fila" in caplog.text


def test_a_mensagem_nova_da_conversa_marcada_tenta_so_o_handoff(sessao, ligada, politica, bug):
    defeito = bug()
    deps = _deps(politica, RESPOSTAS)
    quebra = {"ativa": True}
    nota = deps.chatwoot.nota_privada

    def nota_privada(conversa_id, texto):
        if quebra["ativa"]:
            raise KeyError("bug no handoff")
        nota(conversa_id, texto)

    deps.chatwoot.nota_privada = nota_privada
    _pergunta(sessao, ligada)
    _rodar_ate_o_fim(sessao, ligada, deps, politica.erro_interno_tentativas + 3)
    chamadas = defeito.chamadas

    quebra["ativa"] = False
    ligada.turno_em = SEGUNDA_10H + timedelta(hours=1)
    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert defeito.chamadas == chamadas
    assert len(_handoffs(deps)) == 1
    assert (ligada.motivo_humano, ligada.erros_turno) == (MotivoHumano.ERRO_INTERNO, 0)


def test_o_chatwoot_fora_no_handoff_do_erro_interno_espera_e_sai_um_so_quando_volta(
    sessao, ligada, politica, bug
):
    defeito = bug()
    deps = _deps(politica, RESPOSTAS)
    quebra = {"ativa": True}
    nota = deps.chatwoot.nota_privada

    def nota_privada(conversa_id, texto):
        if quebra["ativa"]:
            raise httpx.ConnectError("o Chatwoot não respondeu")
        nota(conversa_id, texto)

    deps.chatwoot.nota_privada = nota_privada
    _pergunta(sessao, ligada)
    n = politica.erro_interno_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados == [ResultadoTurno.ERRO] * (n + 2)
    assert ligada.turno_em is not None and _handoffs(deps) == []
    # O turno com defeito não roda de novo depois de esgotadas as tentativas.
    assert defeito.chamadas == n

    quebra["ativa"] = False
    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.HANDOFF
    assert len(_handoffs(deps)) == 1
    assert defeito.chamadas == n
    assert ligada.motivo_humano is MotivoHumano.ERRO_INTERNO


def test_o_handoff_do_erro_interno_so_sai_dentro_da_janela(sessao, ligada, politica, bug):
    defeito = bug()
    deps = _deps(politica, RESPOSTAS)
    _pergunta(sessao, ligada)
    sexta_17h59 = SEGUNDA_10H.replace(day=30, hour=17, minute=59)
    ligada.erros_turno = politica.erro_interno_tentativas
    sessao.commit()

    # As tentativas acabaram e o retorno cai depois das 18h: o handoff espera a janela abrir.
    resultado = processar_turno(sessao, ligada.id, sexta_17h59 + timedelta(minutes=2), deps)

    assert resultado is ResultadoTurno.ADIADO
    abertura = SEGUNDA_10H.replace(day=31, hour=8)
    assert ligada.turno_em == abertura and _handoffs(deps) == []

    assert processar_turno(sessao, ligada.id, abertura, deps) is ResultadoTurno.HANDOFF
    assert defeito.chamadas == 0
    assert len(_handoffs(deps)) == 1


def test_o_erro_interno_nao_aciona_o_humano_que_ja_esta_na_conversa(sessao, ligada, politica, bug):
    bug()
    deps = _deps(politica, RESPOSTAS)
    ligada.human_owned = True
    _pergunta(sessao, ligada)
    ligada.erros_turno = politica.erro_interno_tentativas
    sessao.commit()

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert _handoffs(deps) == [] and ligada.erros_turno == 0


def test_em_sombra_depois_das_tentativas_o_turno_e_registrado_e_sai_da_fila(
    sessao, ligada, politica, bug
):
    defeito = bug()
    deps = _deps_sombra(politica)
    _pergunta(sessao, ligada)
    n = politica.erro_interno_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados == [ResultadoTurno.ERRO] * n
    assert defeito.chamadas == n
    assert (ligada.turno_em, ligada.erros_turno) == (None, 0)
    assert ligada.estado is EstadoConversa.INICIADO and ligada.motivo_humano is None
    assert deps.chatwoot.chamadas == [] and deps.chatwoot.enviadas(555) == []
    turnos = sessao.scalars(select(Turno).order_by(Turno.id)).all()
    assert [t.resultado for t in turnos] == [ResultadoTurno.ERRO] * n
    assert all(t.modo is Modo.SOMBRA for t in turnos)
    assert [t.redacao.get("erro") for t in turnos[:-1]] == ["RuntimeError"] * (n - 1)
    # A última linha diz que o turno foi encerrado, e a mensagem do cliente foi consumida.
    assert turnos[-1].redacao["erro"] == MotivoHumano.ERRO_INTERNO.value
    assert ligada.ultima_tomada_id == turnos[-1].mensagens_ids[-1]


def test_em_sombra_antes_do_teto_o_turno_volta_para_a_fila_sem_consumir(
    sessao, ligada, politica, bug
):
    bug()
    deps = _deps_sombra(politica)
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    assert ligada.erros_turno == 1 and ligada.ultima_tomada_id is None
    assert ligada.turno_em == SEGUNDA_10H + timedelta(seconds=politica.integracao_espera_segundos)
