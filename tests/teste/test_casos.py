"""A ativação dos casos do modo teste (D56): o piloto, a trava de subida e o 1º contato."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.integracoes.ctn_teste import CasoInexistente, CTNDeCasos
from agente.politica import Regua
from agente.teste.casos import (
    ativar_caso,
    conversa_viva_do_testador,
    exigir_piloto_fora_da_carteira,
    garantir_caso,
)
from agente.tipos import StatusCobranca
from agente.turno.liberacao import no_piloto
from tests.conftest import TESTADOR_1, TESTADOR_2

HOJE = date(2026, 11, 10)
AGORA = datetime(2026, 11, 10, 13, tzinfo=UTC)


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


def test_no_piloto_vale_com_e_sem_o_9(politica_de_teste):
    assert no_piloto(TESTADOR_1, politica_de_teste)
    assert no_piloto("+558388887777", politica_de_teste)
    assert no_piloto("83 98888-7777", politica_de_teste)
    assert no_piloto(TESTADOR_2, politica_de_teste)


def test_no_piloto_recusa_quem_esta_fora_da_lista_e_o_que_nao_e_telefone(politica_de_teste):
    assert not no_piloto("+5583977776666", politica_de_teste)
    assert not no_piloto("abc", politica_de_teste)
    assert not no_piloto("", politica_de_teste)


def test_no_piloto_sem_o_piloto_na_politica_e_falso(politica):
    assert not no_piloto(TESTADOR_1, politica)


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


def test_a_subida_conta_as_cobrancas_reais_ativas_no_piloto(sessao, politica_de_teste):
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


# ---- ativar_caso e garantir_caso ----


@pytest.fixture
def casos(politica_de_teste):
    return CTNDeCasos(
        politica_de_teste.teste.casos, politica_de_teste.mensalidade_centavos, lambda: HOJE
    )


def _cobrancas(sessao):
    return list(sessao.scalars(select(Cobranca).order_by(Cobranca.id)))


def test_ativar_o_caso_cria_a_cobranca_conferida_de_hoje(sessao, casos, politica_de_teste):
    cobranca = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 3, AGORA)
    sessao.commit()

    assert cobranca.cliente.matricula_ctn == "SIM-3-7777"
    assert (cobranca.cliente.nome, cobranca.cliente.telefone) == ("Carla", TESTADOR_1)
    assert cobranca.campanha == "teste"
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.regua is Regua.R4
    assert (cobranca.mensalidades_abertas, cobranca.mensalidades_retrato) == (4, 4)
    assert cobranca.divida_centavos == 4 * politica_de_teste.mensalidade_centavos
    assert cobranca.ult_mesref == date(2026, 6, 1)
    assert cobranca.conferida_em == AGORA
    assert cobranca.meses_em_aberto == ["2026/07", "2026/08", "2026/09", "2026/10"]
    assert cobranca.propenso_cartao is False


def test_o_caso_2_sai_propenso_ao_cartao_pela_conferencia(sessao, casos, politica_de_teste):
    cobranca = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 2, AGORA)
    assert cobranca.propenso_cartao is True
    assert cobranca.regua is Regua.R3


def test_o_telefone_do_cliente_e_o_do_evento_normalizado(sessao, casos, politica_de_teste):
    cobranca = ativar_caso(sessao, casos, politica_de_teste, "83 98888-7777", 1, AGORA)
    assert cobranca.cliente.telefone == "+5583988887777"
    assert casos.consultar_cliente("SIM-1-7777").telefone == "+5583988887777"


def test_trocar_de_caso_encerra_a_anterior_e_nao_deixa_duas_ativas(
    sessao, casos, politica_de_teste
):
    primeira = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 1, AGORA)
    segunda = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 4, AGORA)
    sessao.commit()

    assert primeira.status is StatusCobranca.ENCERRADA
    assert primeira.encerrada_motivo == "troca_de_caso"
    assert primeira.encerrada_em == AGORA
    assert segunda.status is StatusCobranca.ATIVA
    ativas = [c for c in _cobrancas(sessao) if c.status is StatusCobranca.ATIVA]
    assert ativas == [segunda]


def test_o_mesmo_caso_de_novo_cria_cobranca_nova_para_o_mesmo_cliente(
    sessao, casos, politica_de_teste
):
    primeira = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 3, AGORA)
    casos.pagar_meses("SIM-3-7777", 2, AGORA, "k1")
    segunda = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 3, AGORA)
    sessao.commit()

    assert segunda.id != primeira.id
    assert segunda.cliente_id == primeira.cliente_id
    assert primeira.status is StatusCobranca.ENCERRADA
    # O caso volta ao começo: os 2 meses pagos voltam a estar em aberto.
    assert segunda.mensalidades_abertas == 4
    assert len(sessao.scalars(select(Cliente)).all()) == 1


def test_a_cobranca_paga_do_caso_nao_e_encerrada_de_novo(sessao, casos, politica_de_teste):
    primeira = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 1, AGORA)
    primeira.status = StatusCobranca.PAGA
    ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 2, AGORA)
    assert primeira.status is StatusCobranca.PAGA
    assert primeira.encerrada_motivo is None


def test_a_troca_so_encerra_as_cobrancas_do_telefone_do_testador(sessao, casos, politica_de_teste):
    do_1 = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 1, AGORA)
    do_2 = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_2, 1, AGORA)
    ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 5, AGORA)
    assert do_1.status is StatusCobranca.ENCERRADA
    assert do_2.status is StatusCobranca.ATIVA


def test_a_troca_encerra_a_ativa_escrita_sem_o_9(sessao, casos, politica_de_teste):
    antiga = ativar_caso(sessao, casos, politica_de_teste, "+558388887777", 1, AGORA)
    ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 2, AGORA)
    assert antiga.status is StatusCobranca.ENCERRADA


def test_caso_fora_do_cardapio_e_inexistente_e_nao_grava(sessao, casos, politica_de_teste):
    with pytest.raises(CasoInexistente):
        ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 9, AGORA)
    assert _cobrancas(sessao) == []


def test_garantir_caso_cria_o_caso_padrao_para_o_piloto_sem_cobranca(
    sessao, casos, politica_de_teste
):
    cobranca = garantir_caso(sessao, casos, politica_de_teste, TESTADOR_1, AGORA)
    assert cobranca.cliente.matricula_ctn == "SIM-1-7777"
    assert cobranca.status is StatusCobranca.ATIVA


def test_garantir_caso_nao_faz_nada_com_cobranca_ativa_ou_fora_no_piloto(
    sessao, casos, politica_de_teste
):
    assert garantir_caso(sessao, casos, politica_de_teste, "+5583977776666", AGORA) is None
    ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 3, AGORA)
    assert garantir_caso(sessao, casos, politica_de_teste, TESTADOR_1, AGORA) is None
    assert len(_cobrancas(sessao)) == 1


def test_garantir_caso_com_o_caso_anterior_encerrado_recomeca_no_padrao(
    sessao, casos, politica_de_teste
):
    primeira = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 3, AGORA)
    primeira.status = StatusCobranca.PAGA
    cobranca = garantir_caso(sessao, casos, politica_de_teste, TESTADOR_1, AGORA)
    assert cobranca.cliente.matricula_ctn == "SIM-1-7777"


def test_a_conversa_viva_e_a_de_maior_id_mesmo_com_a_lista_fora_de_ordem(
    sessao, casos, politica_de_teste
):
    cobranca = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_1, 1, AGORA)
    mais_nova = Conversa(id=20, cobranca=cobranca, chatwoot_conversa_id=2020)
    mais_velha = Conversa(id=10, cobranca=cobranca, chatwoot_conversa_id=1010)
    sessao.add_all([mais_nova, mais_velha])
    sessao.flush()
    # A lista `cobranca.conversas` não tem ordem (o banco a devolve como quiser): a de id maior
    # fica na frente, e a escolhida tem de ser a mais nova do mesmo jeito.
    cobranca.conversas.sort(key=lambda conversa: -conversa.id)
    assert [c.id for c in cobranca.conversas] == [20, 10]

    viva = conversa_viva_do_testador(sessao, TESTADOR_1, "teste", 999)

    assert viva is mais_nova
