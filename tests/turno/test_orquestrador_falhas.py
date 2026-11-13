"""Uma integração que falha não refaz o turno sem fim: tentativas, espera e um só handoff (5.3).

A falha do CTN, do Pix ou do Chatwoot no turno ativo desfaz o turno, conta uma tentativa na
conversa e agenda a próxima com uma espera que dobra. Esgotadas as tentativas, sai um handoff só.
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Pagamento, Turno
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.llm_turno import LLMTurnoIndisponivel
from agente.integracoes.pix import PixFalso
from agente.tipos import (
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.orquestrador import processar_turno

from .auxiliares import SEGUNDA_10H, _bruto, _deps

# Um turno desfeito pela falha volta a chamar o Luna: uma resposta para cada tentativa.
RESPOSTAS = [_bruto()] * 10


class CTNFora(CTNFalso):
    """O CTN que falha as primeiras `falhas` vezes que gera um link."""

    def __init__(self, falhas, *args):
        super().__init__(*args)
        self.falhas, self.tentativas = falhas, 0

    def gerar_link_pagamento(self, matricula, valor_centavos, cobranca_id):
        self.tentativas += 1
        if self.tentativas <= self.falhas:
            raise httpx.ConnectError("o CTN não respondeu")
        return super().gerar_link_pagamento(matricula, valor_centavos, cobranca_id)


class PixFora(PixFalso):
    """O banco que falha as primeiras `falhas` vezes que cria uma cobrança."""

    def __init__(self, falhas, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.falhas, self.tentativas = falhas, 0

    def criar_cobranca(self, txid, valor_centavos, validade, devedor_nome):
        self.tentativas += 1
        if self.tentativas <= self.falhas:
            raise httpx.ConnectError("o banco não respondeu")
        return super().criar_cobranca(txid, valor_centavos, validade, devedor_nome)


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _pergunta(sessao, conversa):
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, "quanto eu devo?", "cw-1", False, SEGUNDA_10H
    )


def _handoffs(deps):
    return [c for c in deps.chatwoot.chamadas if c[0] == "mudar_status"]


def _rodar_ate_o_fim(sessao, conversa, deps, vezes):
    """Roda o turno nos horários que a própria conversa agendou; devolve os resultados."""
    resultados, agora = [], SEGUNDA_10H
    for _ in range(vezes):
        resultados.append(processar_turno(sessao, conversa.id, agora, deps))
        if conversa.turno_em is None:
            break
        agora = conversa.turno_em
    return resultados


def test_o_pix_fora_leva_a_um_so_handoff_depois_das_tentativas(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)
    deps.pix = PixFora(10**6)
    _pergunta(sessao, ligada)
    n = politica.integracao_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados == [ResultadoTurno.ERRO] * (n - 1) + [ResultadoTurno.HANDOFF]
    assert deps.pix.tentativas == n
    assert len(_handoffs(deps)) == 1
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.INTEGRACAO_INDISPONIVEL,
    )
    assert (ligada.turno_em, ligada.falhas_turno) == (None, 0)


def test_o_ctn_fora_leva_a_um_so_handoff_depois_das_tentativas(sessao, ligada, politica):
    ligada.cobranca.propenso_cartao = True
    com_link = _bruto(reply="O valor em aberto é {divida}. Pague e cadastre o cartão: {link_ctn}")
    deps = _deps(politica, [com_link] * 10)
    deps.ctn = CTNFora(10**6, deps.ctn.clientes.values())
    _pergunta(sessao, ligada)
    n = politica.integracao_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados[-1] is ResultadoTurno.HANDOFF
    assert len(resultados) == n
    assert deps.ctn.tentativas == n
    assert len(_handoffs(deps)) == 1
    assert ligada.motivo_humano is MotivoHumano.INTEGRACAO_INDISPONIVEL
    assert deps.ctn.links == {}


def test_o_luna_que_falha_e_volta_reagenda_e_envia_a_resposta_sem_handoff(sessao, ligada, politica):
    # AUD-03: falha na 1ª chamada, responde na 2ª; sem handoff, sem fixa e com uma só resposta.
    deps = _deps(politica, [LLMTurnoIndisponivel("fora do ar"), _bruto()])
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
    assert ligada.turno_em == SEGUNDA_10H + timedelta(seconds=politica.integracao_espera_segundos)
    assert deps.chatwoot.enviadas(555) == []

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 100,20" in texto and texto != TEXTO_CANAL_EXTERNO
    assert _handoffs(deps) == []
    assert ligada.falhas_turno == 0 and ligada.estado is not EstadoConversa.ATENDIMENTO_HUMANO
    assert len(deps.llm_turno.pedidos) == 2
    # A rajada não se perdeu: a 2ª chamada leu a mesma pergunta.
    assert "quanto eu devo?" in deps.llm_turno.pedidos[1][1]


def test_o_luna_fora_sem_parar_leva_a_um_so_handoff_depois_das_tentativas(sessao, ligada, politica):
    deps = _deps(politica, [])  # a falsa sem resposta levanta LLMTurnoIndisponivel
    _pergunta(sessao, ligada)
    n = politica.integracao_tentativas

    resultados = _rodar_ate_o_fim(sessao, ligada, deps, n + 2)

    assert resultados == [ResultadoTurno.ERRO] * (n - 1) + [ResultadoTurno.HANDOFF]
    assert len(_handoffs(deps)) == 1
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert ligada.motivo_humano is MotivoHumano.INTEGRACAO_INDISPONIVEL


def test_a_espera_entre_as_tentativas_dobra(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)
    deps.pix = PixFora(10**6)
    _pergunta(sessao, ligada)
    base = politica.integracao_espera_segundos

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    assert ligada.turno_em == SEGUNDA_10H + timedelta(seconds=base)
    assert ligada.falhas_turno == 1

    segunda = ligada.turno_em
    processar_turno(sessao, ligada.id, segunda, deps)
    assert ligada.turno_em == segunda + timedelta(seconds=2 * base)
    assert ligada.falhas_turno == 2


def test_a_falha_nao_consome_a_mensagem_do_cliente_e_fica_no_historico(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)
    deps.pix = PixFora(1)
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    erro = sessao.scalars(select(Turno)).one()
    assert erro.resultado is ResultadoTurno.ERRO
    assert erro.modo is Modo.ATIVO
    assert erro.redacao == {"erro": "ConnectError", "tentativa": 1}
    assert ligada.ultima_tomada_id is None
    assert deps.chatwoot.enviadas(555) == []


def test_a_integracao_que_volta_fecha_o_turno_e_zera_a_conta(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)
    deps.pix = PixFora(1, agora=lambda: SEGUNDA_10H)
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert ligada.falhas_turno == 0
    assert len(deps.pix.cobrancas) == 1
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert len(_handoffs(deps)) == 0


def test_o_chatwoot_fora_no_handoff_espera_mais_a_cada_vez_e_sai_um_so_quando_volta(
    sessao, ligada, politica
):
    retencao = _bruto(quer_retencao=True, acao="NONE", reply="")
    deps = _deps(politica, [retencao] * 10)
    quebra = {"ativa": True}
    nota = deps.chatwoot.nota_privada

    def nota_privada(conversa_id, texto):
        if quebra["ativa"]:
            raise httpx.ConnectError("o Chatwoot não respondeu")
        nota(conversa_id, texto)

    deps.chatwoot.nota_privada = nota_privada
    _pergunta(sessao, ligada)
    base, teto = politica.integracao_espera_segundos, politica.integracao_espera_maxima_segundos

    esperas, agora = [], SEGUNDA_10H
    for _ in range(politica.integracao_tentativas + 3):
        assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.ERRO
        esperas.append(ligada.turno_em - agora)
        agora = ligada.turno_em
    # Passadas as tentativas, ela só tenta o handoff; a espera segue dobrando até o teto.
    assert esperas == [timedelta(seconds=min(base * 2**k, teto)) for k in range(len(esperas))]

    quebra["ativa"] = False
    assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.HANDOFF

    assert len(_handoffs(deps)) == 1
    assert deps.pix.cobrancas == {}
    assert ligada.motivo_humano is MotivoHumano.INTEGRACAO_INDISPONIVEL


def test_o_handoff_das_tentativas_esperadas_so_sai_dentro_da_janela(sessao, ligada, politica):
    deps = _deps(politica, RESPOSTAS)
    deps.pix = PixFora(10**6)
    _pergunta(sessao, ligada)
    sexta_17h59 = SEGUNDA_10H.replace(day=30, hour=17, minute=59)
    ligada.falhas_turno = politica.integracao_tentativas

    # As tentativas acabaram e o retorno cai depois das 18h: o handoff espera a janela abrir.
    resultado = processar_turno(sessao, ligada.id, sexta_17h59 + timedelta(minutes=2), deps)

    assert resultado is ResultadoTurno.ADIADO
    assert ligada.turno_em == SEGUNDA_10H.replace(day=31, hour=8)
    assert _handoffs(deps) == [] and ligada.falhas_turno == politica.integracao_tentativas
