"""Jev 1: a leitura de toda mensagem e os gatilhos de saída (arquitetura, seção 2)."""

import pytest

from agente.integracoes.jev import EstadoJev, JevFalso, responder
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo, MotivoHumano
from agente.turno.jev1 import (
    GATILHOS,
    PERGUNTA_DO_GATILHO,
    PERGUNTAS_JEV1,
    LeituraJev1,
    gatilho_de_saida,
    ler,
)

ESTADO = EstadoJev((("CLIENTE", "oi"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)

ORDEM = [
    MotivoHumano.RETENCAO,
    MotivoHumano.JURIDICO,
    MotivoHumano.CONCILIACAO,
    MotivoHumano.CONTESTACAO,
    MotivoHumano.CAUTELA,
    MotivoHumano.ATRITO,
    MotivoHumano.ENERGISA,
    MotivoHumano.TOM,
]


def _leitura(*regras):
    return ler(JevFalso(list(regras)), ESTADO)


def test_sao_as_13_perguntas_da_arquitetura():
    assert [p.nome for p in PERGUNTAS_JEV1] == [
        "tom",
        "quer_retencao",
        "risco_juridico",
        "alega_pagamento",
        "contesta_divida",
        "terceiro",
        "vulnerabilidade",
        "objecao_valor",
        "atrito_clinica",
        "quer_conta_energia",
        "intencao_pagar",
        "entendeu_proposta",
        "repete_sem_avancar",
    ]
    tom = PERGUNTAS_JEV1[0]
    assert tom.tipo == "score"
    assert tom.opcoes == ("cordial", "neutro", "impaciente", "irritado", "hostil")
    assert PERGUNTAS_JEV1[10].opcoes == ("nenhuma", "hesitante", "disposto", "pronto")


def test_ler_devolve_um_laudo_por_pergunta(politica):
    leitura = _leitura(responder("objecao_valor", True, 0.88))
    assert len(leitura.laudos) == 13
    assert leitura.sim("objecao_valor")
    assert leitura.laudo("objecao_valor") == Laudo("objecao_valor", True, 0.88, 0.90)
    assert leitura.tom == "cordial"


def test_a_politica_tem_o_limiar_de_todo_gatilho(politica):
    assert set(GATILHOS.values()) <= set(politica.limiares_jev1)


@pytest.mark.parametrize(("motivo", "pergunta"), list(GATILHOS.items()))
def test_cada_gatilho_no_limite(politica, motivo, pergunta):
    limiar = politica.limiares_jev1[pergunta]

    abaixo = _leitura(responder(pergunta, True, round(limiar - 0.01, 2)))
    no_limite = _leitura(responder(pergunta, True, limiar))

    assert gatilho_de_saida(abaixo, politica) is None
    assert gatilho_de_saida(no_limite, politica) is motivo


def test_limites_da_arquitetura(politica):
    # 0,69 não dispara e 0,70 dispara, nos gatilhos de limiar 0,70.
    assert gatilho_de_saida(_leitura(responder("risco_juridico", True, 0.69)), politica) is None
    juridico = _leitura(responder("risco_juridico", True, 0.70))
    assert gatilho_de_saida(juridico, politica) is MotivoHumano.JURIDICO


def test_resposta_nao_com_probabilidade_alta_nao_dispara(politica):
    leitura = _leitura(responder("quer_retencao", False, 0.99))
    assert gatilho_de_saida(leitura, politica) is None


@pytest.mark.parametrize("tom", ["irritado", "hostil"])
def test_tom_irritado_ou_hostil_dispara(politica, tom):
    assert gatilho_de_saida(_leitura(responder("tom", tom)), politica) is MotivoHumano.TOM


@pytest.mark.parametrize("tom", ["cordial", "neutro", "impaciente"])
def test_os_outros_tons_nao_disparam(politica, tom):
    assert gatilho_de_saida(_leitura(responder("tom", tom)), politica) is None


def test_com_varios_vence_o_primeiro_da_ordem(politica):
    todas = [responder(pergunta, True, 0.99) for pergunta in GATILHOS.values()]
    todas.append(responder("tom", "hostil"))
    for motivo in ORDEM:
        assert gatilho_de_saida(_leitura(*todas), politica) is motivo
        # Tira o vencedor e confere o seguinte.
        todas = [r for r in todas if r(_pergunta(PERGUNTA_DO_GATILHO[motivo]), ESTADO) is None]
    assert gatilho_de_saida(_leitura(*todas), politica) is None


def test_laudo_do_gatilho_para_a_auditoria(politica):
    leitura = _leitura(responder("alega_pagamento", True, 0.93, 0.81))
    motivo = gatilho_de_saida(leitura, politica)
    assert leitura.laudo(PERGUNTA_DO_GATILHO[motivo]) == Laudo("alega_pagamento", True, 0.93, 0.81)


def test_leitura_sem_a_pergunta_falha():
    with pytest.raises(KeyError):
        LeituraJev1(()).laudo("tom")


def _pergunta(nome):
    return next(p for p in PERGUNTAS_JEV1 if p.nome == nome)
