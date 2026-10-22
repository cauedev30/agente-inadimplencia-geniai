"""Jev 3: a validação do texto antes do envio (arquitetura, seção 6)."""

import pytest

from agente.integracoes.jev import EstadoJev, JevFalso, responder
from agente.politica import Regua
from agente.tipos import EstadoConversa
from agente.turno.acoes import Acao
from agente.turno.jev3 import DEVEM_DAR_NAO, DEVEM_DAR_SIM, PERGUNTAS_JEV3, validar

ESTADO = EstadoJev((("CLIENTE", "quanto devo?"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)
TEXTO = "Olá! O valor em aberto é R$ 100,20. Pode pagar pelo Pix abaixo."
ACOES = [Acao.INFORM_DEBT, Acao.GENERATE_PIX]
NUMEROS = {"divida_centavos": 10020}


def _aprovadas():
    """As respostas de um texto bom: sim nas de sim, não nas de não."""
    return [responder(nome, True) for nome in DEVEM_DAR_SIM] + [
        responder(nome, False) for nome in DEVEM_DAR_NAO
    ]


def test_sao_as_8_perguntas_da_arquitetura():
    assert [p.nome for p in PERGUNTAS_JEV3] == [
        "bate_com_acao",
        "valor_nao_autorizado",
        "promessa_indevida",
        "tom_adequado",
        "expoe_terceiro",
        "responde_a_pergunta",
        "pt_br_natural",
        "vazou_instrucao",
    ]
    assert all(p.tipo == "bool" for p in PERGUNTAS_JEV3)
    assert set(DEVEM_DAR_SIM) == {
        "bate_com_acao",
        "tom_adequado",
        "responde_a_pergunta",
        "pt_br_natural",
    }
    assert set(DEVEM_DAR_NAO) == {
        "valor_nao_autorizado",
        "promessa_indevida",
        "expoe_terceiro",
        "vazou_instrucao",
    }


def test_texto_bom_e_aprovado():
    validacao = validar(JevFalso(_aprovadas()), TEXTO, ACOES, NUMEROS, ESTADO)
    assert validacao.aprovado
    assert validacao.reprovadas == []
    assert len(validacao.laudos) == 8


@pytest.mark.parametrize("pergunta", DEVEM_DAR_SIM)
def test_cada_pergunta_de_sim_reprova_sozinha(pergunta):
    jev = JevFalso([responder(pergunta, False), *_aprovadas()])
    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)
    assert not validacao.aprovado
    assert validacao.reprovadas == [pergunta]


@pytest.mark.parametrize("pergunta", DEVEM_DAR_NAO)
def test_cada_pergunta_de_nao_reprova_sozinha(pergunta):
    jev = JevFalso([responder(pergunta, True), *_aprovadas()])
    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)
    assert not validacao.aprovado
    assert validacao.reprovadas == [pergunta]


def test_reprovadas_lista_todas_na_ordem_das_perguntas():
    jev = JevFalso(
        [responder("vazou_instrucao", True), responder("bate_com_acao", False), *_aprovadas()]
    )
    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)
    assert validacao.reprovadas == ["bate_com_acao", "vazou_instrucao"]


def test_o_jev3_le_o_texto_as_acoes_e_os_numeros():
    jev = JevFalso(_aprovadas())
    validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)
    _, estado = jev.chamadas[-1]
    assert estado.texto_candidato == TEXTO
    assert estado.acoes_do_turno == ("INFORM_DEBT", "GENERATE_PIX")
    assert estado.numeros == NUMEROS
    assert estado.historico == ESTADO.historico
