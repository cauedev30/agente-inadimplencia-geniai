"""A liberação gradual (tarefa 5.5): da sombra ao piloto, a uma parte e à carteira toda."""

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cliente, Cobranca, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.politica import (
    LiberacaoPercentual,
    LiberacaoPiloto,
    LiberacaoSombra,
    PoliticaInvalida,
    Regua,
    carregar_politica,
)
from agente.tipos import Modo, OrigemMensagem, ResultadoTurno
from agente.turno.liberacao import liberar_tudo, modo_da_conversa
from agente.turno.orquestrador import Dependencias, processar_turno

from ..conftest import RAIZ
from .auxiliares import SEGUNDA_10H, _bruto

EQUIPE = "+5583900000001"


def _cobranca(id_=1, telefone=EQUIPE):
    cliente = Cliente(matricula_ctn=f"M-{id_}", nome="Cliente Fictício", telefone=telefone)
    return Cobranca(id=id_, cliente=cliente, regua=Regua.R3, divida_centavos=10020)


def _com(politica, liberacao):
    return politica.model_copy(update={"liberacao": liberacao})


def test_o_padrao_do_arquivo_e_a_sombra(politica):
    assert politica.liberacao == LiberacaoSombra()
    assert modo_da_conversa(_cobranca(), politica) is Modo.SOMBRA


def test_no_piloto_so_os_telefones_da_lista_ficam_ativos(politica):
    piloto = _com(politica, LiberacaoPiloto(modo="piloto", telefones=frozenset({EQUIPE})))
    assert modo_da_conversa(_cobranca(telefone=EQUIPE), piloto) is Modo.ATIVO
    assert modo_da_conversa(_cobranca(telefone="+5583900000002"), piloto) is Modo.SOMBRA


@pytest.mark.parametrize(
    ("guardado", "da_lista"),
    [("+5583999990001", "+558399990001"), ("+558399990001", "+5583999990001")],
)
def test_no_piloto_o_telefone_casa_com_e_sem_o_9(politica, guardado, da_lista):
    """A1: o CTN dá o telefone com o 9 e a lista do piloto pode ter sido escrita sem ele."""
    piloto = _com(politica, LiberacaoPiloto(modo="piloto", telefones=frozenset({da_lista})))
    assert modo_da_conversa(_cobranca(telefone=guardado), piloto) is Modo.ATIVO


def test_o_percentual_e_estavel_para_a_mesma_cobranca(politica):
    parte = _com(politica, LiberacaoPercentual(modo="percentual", percentual=30))
    modos = {modo_da_conversa(_cobranca(id_=7), parte) for _ in range(20)}
    assert len(modos) == 1


def test_o_percentual_libera_mais_ou_menos_a_parte_pedida(politica):
    parte = _com(politica, LiberacaoPercentual(modo="percentual", percentual=30))
    ativas = sum(modo_da_conversa(_cobranca(id_=i), parte) is Modo.ATIVO for i in range(1, 2001))
    assert 500 <= ativas <= 700


def test_subir_o_percentual_nao_devolve_ninguem_para_a_sombra(politica):
    dez = _com(politica, LiberacaoPercentual(modo="percentual", percentual=10))
    vinte = _com(politica, LiberacaoPercentual(modo="percentual", percentual=20))
    for i in range(1, 500):
        if modo_da_conversa(_cobranca(id_=i), dez) is Modo.ATIVO:
            assert modo_da_conversa(_cobranca(id_=i), vinte) is Modo.ATIVO


@pytest.mark.parametrize(("percentual", "modo"), [(0, Modo.SOMBRA), (100, Modo.ATIVO)])
def test_os_extremos_do_percentual(politica, percentual, modo):
    liberacao = LiberacaoPercentual(modo="percentual", percentual=percentual)
    todas = {modo_da_conversa(_cobranca(id_=i), _com(politica, liberacao)) for i in range(1, 300)}
    assert todas == {modo}


def test_liberar_tudo_abre_a_carteira_toda(politica):
    assert modo_da_conversa(_cobranca(), liberar_tudo(politica)) is Modo.ATIVO


def _politica_com(tmp_path, liberacao: str):
    texto = (RAIZ / "politica.toml").read_text(encoding="utf-8")
    assert 'liberacao = "sombra"' in texto
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto.replace('liberacao = "sombra"', liberacao), encoding="utf-8")
    return carregar_politica(caminho)


def test_mudar_a_liberacao_e_so_mudar_o_arquivo(tmp_path):
    piloto = _politica_com(tmp_path, f'liberacao = {{ modo = "piloto", telefones = ["{EQUIPE}"] }}')
    assert piloto.liberacao == LiberacaoPiloto(modo="piloto", telefones=frozenset({EQUIPE}))
    parte = _politica_com(tmp_path, 'liberacao = { modo = "percentual", percentual = 25 }')
    assert parte.liberacao == LiberacaoPercentual(modo="percentual", percentual=25)


@pytest.mark.parametrize(
    "liberacao",
    [
        'liberacao = "piloto"',  # o piloto precisa da lista
        'liberacao = { modo = "piloto", telefones = [] }',
        'liberacao = { modo = "percentual", percentual = 150 }',
        'liberacao = "carteira"',
    ],
)
def test_liberacao_invalida_e_recusada(tmp_path, liberacao):
    with pytest.raises(PoliticaInvalida, match="liberacao"):
        _politica_com(tmp_path, liberacao)


# No turno: o Config em sombra força a sombra; no ativo, a liberação decide a conversa.


def _deps(politica, modo):
    return Dependencias(
        politica=politica,
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        llm_turno=LLMTurnoFalso(respostas=[_bruto()]),
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _turno(sessao, ligada, deps):
    registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.CLIENTE, "quanto devo?", "cw-1", False, SEGUNDA_10H
    )
    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    return resultado, sessao.scalars(select(Turno)).one()


def test_telefone_fora_do_piloto_fica_em_sombra(sessao, ligada, politica):
    piloto = _com(politica, LiberacaoPiloto(modo="piloto", telefones=frozenset({"+5583999999999"})))
    deps = _deps(piloto, Modo.ATIVO)

    resultado, turno = _turno(sessao, ligada, deps)

    assert (resultado, turno.modo) == (ResultadoTurno.SOMBRA, Modo.SOMBRA)
    assert deps.chatwoot.chamadas == []


def test_telefone_do_piloto_fica_ativo(sessao, ligada, politica):
    piloto = _com(politica, LiberacaoPiloto(modo="piloto", telefones=frozenset({EQUIPE})))
    deps = _deps(piloto, Modo.ATIVO)

    resultado, turno = _turno(sessao, ligada, deps)

    assert (resultado, turno.modo) == (ResultadoTurno.ENVIADO, Modo.ATIVO)
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_o_config_em_sombra_forca_a_sombra_em_tudo(sessao, ligada, politica):
    deps = _deps(liberar_tudo(politica), Modo.SOMBRA)

    resultado, turno = _turno(sessao, ligada, deps)

    assert (resultado, turno.modo) == (ResultadoTurno.SOMBRA, Modo.SOMBRA)
    assert deps.chatwoot.chamadas == []


def test_os_telefones_do_piloto_sao_normalizados_ao_carregar(tmp_path):
    piloto = _politica_com(
        tmp_path, 'liberacao = { modo = "piloto", telefones = ["+55 83 90000-0001"] }'
    )
    assert piloto.liberacao.telefones == frozenset({EQUIPE})


@pytest.mark.parametrize("telefone", ["8390000", "+1 202 555 0100", ""])
def test_telefone_invalido_no_piloto_e_recusado(tmp_path, telefone):
    with pytest.raises(PoliticaInvalida, match="telefones"):
        _politica_com(tmp_path, f'liberacao = {{ modo = "piloto", telefones = ["{telefone}"] }}')
