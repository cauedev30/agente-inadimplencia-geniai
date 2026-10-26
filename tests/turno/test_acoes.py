"""As regras de ação: limiares, pré-condições e a tabela de anexos (arquitetura, seção 4; D37)."""

import pytest

from agente.db.modelos import Cobranca, Conversa
from agente.integracoes.jev import EstadoJev, JevFalso, responder
from agente.politica import Regua
from agente.tipos import EstadoConversa, MotivoHumano
from agente.turno.acoes import (
    Acao,
    Decisao,
    anexar,
    aplicar_limiares,
    aplicar_precondicoes,
    autorizar,
)
from agente.turno.jev1 import ler

ESTADO = EstadoJev((("CLIENTE", "oi"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)
COM_LIMIAR = [acao for acao in Acao if acao is not Acao.HUMAN]


def _cobranca(regua=Regua.R3, mensalidades=3, propenso=False):
    return Cobranca(
        regua=regua,
        mensalidades_abertas=mensalidades,
        divida_centavos=3340 * mensalidades,
        propenso_cartao=propenso,
    )


def _conversa(degrau=0, objecoes=0):
    return Conversa(degrau_atual=degrau, objecoes_sem_desconto=objecoes)


def _leitura(objecao=False):
    return ler(JevFalso([responder("objecao_valor", objecao)]), ESTADO)


# Tabela de anexos (D37).


def test_inform_debt_do_propenso_leva_o_link():
    assert anexar(Acao.INFORM_DEBT, _cobranca(propenso=True)) == [Acao.SEND_CTN_LINK]


def test_inform_debt_do_nao_propenso_leva_o_pix():
    assert anexar(Acao.INFORM_DEBT, _cobranca(propenso=False)) == [Acao.GENERATE_PIX]


@pytest.mark.parametrize("propenso", [True, False])
def test_offer_discount_sempre_leva_o_link(propenso):
    assert anexar(Acao.OFFER_DISCOUNT, _cobranca(propenso=propenso)) == [Acao.SEND_CTN_LINK]


@pytest.mark.parametrize(
    "acao",
    [Acao.SEND_CTN_LINK, Acao.GENERATE_PIX, Acao.OUT_OF_SCOPE, Acao.NONE, Acao.HUMAN],
)
@pytest.mark.parametrize("propenso", [True, False])
def test_as_outras_acoes_vao_sem_anexo(acao, propenso):
    assert anexar(acao, _cobranca(propenso=propenso)) == []


# Limiares (arquitetura, seção 4).


def test_a_politica_tem_o_limiar_de_toda_acao(politica):
    assert set(politica.limiares_jev2) == {acao.value for acao in COM_LIMIAR}


@pytest.mark.parametrize("acao", COM_LIMIAR)
def test_limiar_de_cada_acao(politica, acao):
    limiar = politica.limiares_jev2[acao.value]
    assert aplicar_limiares(Decisao(acao, limiar, 0.90), politica) is acao
    abaixo = Decisao(acao, round(limiar - 0.01, 2), 0.90)
    assert aplicar_limiares(abaixo, politica) is MotivoHumano.BAIXA_CONFIANCA


def test_limiares_da_arquitetura(politica):
    assert aplicar_limiares(Decisao(Acao.GENERATE_PIX, 0.89, 0.9), politica) is (
        MotivoHumano.BAIXA_CONFIANCA
    )
    assert aplicar_limiares(Decisao(Acao.GENERATE_PIX, 0.90, 0.9), politica) is Acao.GENERATE_PIX


@pytest.mark.parametrize("acao", list(Acao))
def test_confianca_abaixo_da_minima_vai_para_o_humano(politica, acao):
    decisao = Decisao(acao, 0.99, 0.59)
    assert aplicar_limiares(decisao, politica) is MotivoHumano.BAIXA_CONFIANCA


def test_confianca_na_minima_passa(politica):
    assert aplicar_limiares(Decisao(Acao.NONE, 0.99, 0.60), politica) is Acao.NONE


def test_human_vai_para_o_humano_pela_decisao_do_jev2(politica):
    assert aplicar_limiares(Decisao(Acao.HUMAN, 0.40, 0.90), politica) is MotivoHumano.DECISAO_JEV2


# Pré-condições (arquitetura, seção 4).


def test_offer_discount_com_objecao_e_degrau_seguinte_passa(politica):
    acao = aplicar_precondicoes(
        Acao.OFFER_DISCOUNT, _conversa(degrau=2), _cobranca(), _leitura(objecao=True), politica
    )
    assert acao is Acao.OFFER_DISCOUNT


def test_offer_discount_sem_degrau_seguinte_e_barrado(politica):
    n_degraus = politica.reguas[Regua.R3].n_degraus
    acao = aplicar_precondicoes(
        Acao.OFFER_DISCOUNT,
        _conversa(degrau=n_degraus),
        _cobranca(),
        _leitura(objecao=True),
        politica,
    )
    assert acao is MotivoHumano.OBJECAO_SEM_DESCONTO


@pytest.mark.parametrize("escolhida", [Acao.INFORM_DEBT, Acao.GENERATE_PIX, Acao.NONE])
def test_objecao_com_os_degraus_esgotados_vai_para_o_humano_seja_qual_for_a_acao(
    politica, escolhida
):
    # Como na NR (D38): o cliente que objeta de novo depois do último degrau não segue no robô.
    n_degraus = politica.reguas[Regua.R3].n_degraus
    acao = aplicar_precondicoes(
        escolhida, _conversa(degrau=n_degraus), _cobranca(), _leitura(objecao=True), politica
    )
    assert acao is MotivoHumano.OBJECAO_SEM_DESCONTO


def test_sem_objecao_com_os_degraus_esgotados_as_acoes_passam(politica):
    n_degraus = politica.reguas[Regua.R3].n_degraus
    acao = aplicar_precondicoes(
        Acao.INFORM_DEBT, _conversa(degrau=n_degraus), _cobranca(), _leitura(), politica
    )
    assert acao is Acao.INFORM_DEBT


def test_objecao_com_degrau_seguinte_o_inform_debt_passa(politica):
    acao = aplicar_precondicoes(
        Acao.INFORM_DEBT, _conversa(degrau=1), _cobranca(), _leitura(objecao=True), politica
    )
    assert acao is Acao.INFORM_DEBT


def test_offer_discount_sem_objecao_e_barrado(politica):
    acao = aplicar_precondicoes(
        Acao.OFFER_DISCOUNT, _conversa(), _cobranca(), _leitura(objecao=False), politica
    )
    assert acao is MotivoHumano.BAIXA_CONFIANCA


@pytest.mark.parametrize("regua", [Regua.NR, Regua.R1, Regua.R2])
def test_1a_objecao_na_regua_sem_desconto_vira_inform_debt(politica, regua):
    acao = aplicar_precondicoes(
        Acao.OFFER_DISCOUNT, _conversa(), _cobranca(regua), _leitura(objecao=True), politica
    )
    assert acao is Acao.INFORM_DEBT


@pytest.mark.parametrize("escolhida", [Acao.OFFER_DISCOUNT, Acao.INFORM_DEBT, Acao.GENERATE_PIX])
def test_2a_objecao_na_nr_vai_para_o_humano(politica, escolhida):
    anteriores = politica.objecoes_sem_desconto_ate_humano - 1
    acao = aplicar_precondicoes(
        escolhida,
        _conversa(objecoes=anteriores),
        _cobranca(Regua.NR, mensalidades=1),
        _leitura(objecao=True),
        politica,
    )
    assert acao is MotivoHumano.OBJECAO_SEM_DESCONTO


def test_sem_objecao_na_nr_as_acoes_passam(politica):
    acao = aplicar_precondicoes(
        Acao.INFORM_DEBT, _conversa(objecoes=1), _cobranca(Regua.NR), _leitura(), politica
    )
    assert acao is Acao.INFORM_DEBT


@pytest.mark.parametrize("acao", [a for a in Acao if a is not Acao.OFFER_DISCOUNT])
def test_as_outras_acoes_passam_na_regua_com_desconto(politica, acao):
    resultado = aplicar_precondicoes(acao, _conversa(), _cobranca(), _leitura(True), politica)
    assert resultado is acao


# As três juntas: nenhuma ação sai sem passar por elas.


def test_autorizar_passa_pelos_limiares(politica):
    decisao = Decisao(Acao.OFFER_DISCOUNT, 0.79, 0.9)
    resultado = autorizar(decisao, _conversa(), _cobranca(), _leitura(True), politica)
    assert resultado == (MotivoHumano.BAIXA_CONFIANCA, [])


def test_autorizar_passa_pelas_precondicoes_e_anexa(politica):
    decisao = Decisao(Acao.OFFER_DISCOUNT, 0.95, 0.9)
    nr = _cobranca(Regua.NR, mensalidades=1)
    resultado = autorizar(decisao, _conversa(), nr, _leitura(True), politica)
    # Na NR, a 1ª objeção vira o débito, e o não propenso recebe o Pix junto.
    assert resultado == (Acao.INFORM_DEBT, [Acao.GENERATE_PIX])


def test_autorizar_desconto_vai_com_o_link(politica):
    decisao = Decisao(Acao.OFFER_DISCOUNT, 0.95, 0.9)
    resultado = autorizar(decisao, _conversa(), _cobranca(), _leitura(True), politica)
    assert resultado == (Acao.OFFER_DISCOUNT, [Acao.SEND_CTN_LINK])
