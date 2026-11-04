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


def test_sao_as_9_perguntas_da_arquitetura():
    assert [p.nome for p in PERGUNTAS_JEV3] == [
        "bate_com_acao",
        "valor_nao_autorizado",
        "promessa_indevida",
        "afirma_fato_sem_fonte",
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
        "afirma_fato_sem_fonte",
        "expoe_terceiro",
        "vazou_instrucao",
    }


def test_texto_bom_e_aprovado():
    validacao = validar(JevFalso(_aprovadas()), TEXTO, ACOES, NUMEROS, ESTADO)
    assert validacao.aprovado
    assert validacao.reprovadas == []
    assert len(validacao.laudos) == 9


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
    jev = JevFalso(_aprovadas(), registrar=True)
    validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)
    _, estado = jev.chamadas[-1]
    assert estado.texto_candidato == TEXTO
    assert estado.acoes_do_turno == ("INFORM_DEBT", "GENERATE_PIX")
    assert estado.numeros == NUMEROS
    assert estado.historico == ESTADO.historico


BR_CODE = "00020126580014br.gov.bcb.pix0136a1b2c3d4-e5f6-7a8b-9c0d-e1f2a3b4c5d6520400005303986"


def test_o_jev3_nao_ve_o_codigo_pix_nem_no_texto_nem_nos_numeros():
    # A sequência longa de letras e números faz o Jev 3 reprovar o "português" (A6, 2026-11-04).
    texto = f"Você tem R$ 100,20 em aberto. Para pagar via Pix, use este código: {BR_CODE}"
    numeros = {"divida_centavos": 10020, "br_code": BR_CODE}
    jev = JevFalso(_aprovadas(), registrar=True)

    validar(jev, texto, ACOES, numeros, ESTADO)

    _, estado = jev.chamadas[-1]
    assert BR_CODE not in estado.texto_candidato
    assert estado.texto_candidato.endswith("use este código: [código Pix]")
    assert estado.numeros == {"divida_centavos": 10020, "br_code": "[código Pix]"}
    # O dicionário do turno não é alterado: o cliente recebe o código verdadeiro.
    assert numeros["br_code"] == BR_CODE


def test_o_texto_sem_pix_segue_igual_para_o_jev3():
    jev = JevFalso(_aprovadas(), registrar=True)
    validar(jev, TEXTO, ACOES, {"divida_centavos": 10020}, ESTADO)
    _, estado = jev.chamadas[-1]
    assert estado.texto_candidato == TEXTO
    assert estado.numeros == {"divida_centavos": 10020}


def test_o_texto_que_afirma_fato_sem_fonte_e_reprovado():
    texto = "Sim, o cartão vale para dentista! O valor em aberto é R$ 100,20."
    jev = JevFalso([responder("afirma_fato_sem_fonte", True), *_aprovadas()])
    validacao = validar(jev, texto, ACOES, NUMEROS, ESTADO)
    assert not validacao.aprovado
    assert validacao.reprovadas == ["afirma_fato_sem_fonte"]


def test_fora_do_escopo_nao_reprova_por_nao_responder_a_pergunta():
    # OUT_OF_SCOPE manda não responder o que não veio do backend: `responde_a_pergunta = não` é o
    # esperado. A pergunta continua no laudo, para a calibração.
    jev = JevFalso([responder("responde_a_pergunta", False), *_aprovadas()])

    validacao = validar(jev, TEXTO, [Acao.OUT_OF_SCOPE], NUMEROS, ESTADO)

    assert validacao.aprovado
    assert validacao.reprovadas == []
    assert len(validacao.laudos) == 9
    [laudo] = [x for x in validacao.laudos if x.pergunta == "responde_a_pergunta"]
    assert laudo.valor is False


def test_fora_do_escopo_ainda_reprova_pelas_outras_perguntas():
    jev = JevFalso([responder("pt_br_natural", False), *_aprovadas()])

    validacao = validar(jev, TEXTO, [Acao.OUT_OF_SCOPE], NUMEROS, ESTADO)

    assert validacao.reprovadas == ["pt_br_natural"]


def test_informar_a_divida_continua_reprovando_por_nao_responder_a_pergunta():
    jev = JevFalso([responder("responde_a_pergunta", False), *_aprovadas()])

    validacao = validar(jev, TEXTO, [Acao.INFORM_DEBT], NUMEROS, ESTADO)

    assert validacao.reprovadas == ["responde_a_pergunta"]


QUALIDADE = ["pt_br_natural", "responde_a_pergunta", "bate_com_acao"]


@pytest.mark.parametrize("pergunta", QUALIDADE)
def test_pergunta_de_qualidade_com_nao_abaixo_do_limiar_aprova(pergunta):
    # O "não" das perguntas de qualidade fica perto de 0,5 em texto bom (2º e 3º ensaios, E2.8 e
    # E2.9): só reprova com p ≥ 0,70. O laudo continua gravado, para a calibração.
    jev = JevFalso([responder(pergunta, False, 0.69), *_aprovadas()])

    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)

    assert validacao.aprovado
    [laudo] = [x for x in validacao.laudos if x.pergunta == pergunta]
    assert laudo.valor is False


@pytest.mark.parametrize("pergunta", QUALIDADE)
def test_pergunta_de_qualidade_com_nao_no_limiar_reprova(pergunta):
    jev = JevFalso([responder(pergunta, False, 0.70), *_aprovadas()])

    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)

    assert validacao.reprovadas == [pergunta]


@pytest.mark.parametrize("pergunta", ["valor_nao_autorizado", "promessa_indevida"])
def test_pergunta_de_seguranca_reprova_mesmo_em_duvida(pergunta):
    jev = JevFalso([responder(pergunta, True, 0.55), *_aprovadas()])

    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)

    assert validacao.reprovadas == [pergunta]


@pytest.mark.parametrize("pergunta", ["tom_adequado"])
def test_tom_reprova_mesmo_em_duvida(pergunta):
    jev = JevFalso([responder(pergunta, False, 0.55), *_aprovadas()])

    validacao = validar(jev, TEXTO, ACOES, NUMEROS, ESTADO)

    assert validacao.reprovadas == [pergunta]
