"""A ativação dos casos do modo teste (D56): o piloto, a trava de subida e o 1º contato."""

import pytest

from agente.db.modelos import Cliente, Cobranca
from agente.politica import Regua
from agente.teste.casos import do_piloto, exigir_piloto_fora_da_carteira
from agente.tipos import StatusCobranca
from tests.conftest import TESTADOR_1, TESTADOR_2


def _cobranca(sessao, telefone, campanha, *, matricula, status=StatusCobranca.ATIVA):
    cliente = Cliente(matricula_ctn=matricula, nome="Fictício", telefone=telefone)
    sessao.add(
        Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha=campanha,
            status=status,
        )
    )
    sessao.commit()


def test_do_piloto_vale_com_e_sem_o_9(politica_de_teste):
    assert do_piloto(TESTADOR_1, politica_de_teste)
    assert do_piloto("+558388887777", politica_de_teste)
    assert do_piloto("83 98888-7777", politica_de_teste)
    assert do_piloto(TESTADOR_2, politica_de_teste)


def test_do_piloto_recusa_quem_esta_fora_da_lista_e_o_que_nao_e_telefone(politica_de_teste):
    assert not do_piloto("+5583977776666", politica_de_teste)
    assert not do_piloto("abc", politica_de_teste)
    assert not do_piloto("", politica_de_teste)


def test_do_piloto_sem_o_piloto_na_politica_e_falso(politica):
    assert not do_piloto(TESTADOR_1, politica)


def test_a_subida_aceita_o_piloto_sem_cobranca_ou_so_com_a_do_teste(sessao, politica_de_teste):
    exigir_piloto_fora_da_carteira(sessao, politica_de_teste)
    _cobranca(sessao, TESTADOR_1, "teste", matricula="SIM-1-7777")
    exigir_piloto_fora_da_carteira(sessao, politica_de_teste)


def test_a_subida_recusa_o_telefone_do_piloto_com_cobranca_real_ativa(sessao, politica_de_teste):
    _cobranca(sessao, TESTADOR_1, "ctn", matricula="REAL-1")
    with pytest.raises(ValueError, match="1") as erro:
        exigir_piloto_fora_da_carteira(sessao, politica_de_teste)
    # A mensagem não leva telefone nem nome.
    assert "7777" not in str(erro.value)
    assert "Fictício" not in str(erro.value)


def test_a_subida_pega_o_telefone_do_piloto_escrito_sem_o_9(sessao, politica_de_teste):
    _cobranca(sessao, "+558388887777", "ctn", matricula="REAL-1")
    with pytest.raises(ValueError):
        exigir_piloto_fora_da_carteira(sessao, politica_de_teste)


def test_a_subida_conta_as_cobrancas_reais_ativas_do_piloto(sessao, politica_de_teste):
    _cobranca(sessao, TESTADOR_1, "ctn", matricula="REAL-1")
    _cobranca(sessao, TESTADOR_2, "ctn", matricula="REAL-2")
    with pytest.raises(ValueError, match="2 cobran"):
        exigir_piloto_fora_da_carteira(sessao, politica_de_teste)


def test_a_subida_ignora_a_cobranca_real_que_nao_esta_ativa_e_o_telefone_de_fora(
    sessao, politica_de_teste
):
    _cobranca(sessao, TESTADOR_1, "ctn", matricula="REAL-1", status=StatusCobranca.ENCERRADA)
    _cobranca(sessao, "+5583977776666", "ctn", matricula="REAL-2")
    exigir_piloto_fora_da_carteira(sessao, politica_de_teste)
