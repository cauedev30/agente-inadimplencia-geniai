"""Jev 2: a decisão da ação principal, uma pergunta de escolha (arquitetura, seção 4)."""

from agente.integracoes.jev import EstadoJev, JevFalso, responder
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo
from agente.turno.acoes import Acao, Decisao
from agente.turno.jev1 import ler
from agente.turno.jev2 import PERGUNTAS_JEV2, decidir

ESTADO = EstadoJev((("CLIENTE", "tem desconto?"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)


def test_a_pergunta_e_uma_escolha_entre_as_acoes():
    [pergunta] = PERGUNTAS_JEV2
    assert (pergunta.nome, pergunta.tipo) == ("acao", "escolha")
    assert pergunta.opcoes == tuple(acao.value for acao in Acao)


def test_decidir_devolve_a_acao_com_probabilidade_e_confianca():
    jev = JevFalso([responder("acao", "OFFER_DISCOUNT", 0.86, 0.72)])
    leitura = ler(jev, ESTADO)

    decisao = decidir(jev, ESTADO, leitura)

    assert decisao == Decisao(Acao.OFFER_DISCOUNT, 0.86, 0.72)
    assert decisao.laudo == Laudo("acao", "OFFER_DISCOUNT", 0.86, 0.72)


def test_o_jev2_le_o_estado_com_a_leitura_do_jev1():
    jev = JevFalso([responder("objecao_valor", True)])
    leitura = ler(jev, ESTADO)

    decidir(jev, ESTADO, leitura)

    perguntas, estado = jev.chamadas[-1]
    assert perguntas == ["acao"]
    assert estado.historico == ESTADO.historico
    assert estado.leitura == leitura.laudos
