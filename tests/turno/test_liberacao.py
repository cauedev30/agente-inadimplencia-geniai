"""Quem conversa com o agente (D61): só os telefones do piloto; o resto é ignorado."""

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.politica import LiberacaoPiloto, PoliticaInvalida, carregar_politica
from agente.tipos import Modo, OrigemMensagem, ResultadoTurno
from agente.turno.fila import mensagens_pendentes
from agente.turno.liberacao import no_piloto
from agente.turno.orquestrador import Dependencias, processar_turno

from ..conftest import RAIZ, piloto_com
from .auxiliares import SEGUNDA_10H, _bruto

EQUIPE = "+5583900000001"


def test_o_padrao_do_arquivo_e_o_piloto_vazio(politica):
    assert politica.liberacao == LiberacaoPiloto(modo="piloto", telefones=frozenset())
    assert not no_piloto(EQUIPE, politica)


def test_no_piloto_so_os_telefones_da_lista_conversam(politica):
    piloto = piloto_com(politica, EQUIPE)
    assert no_piloto(EQUIPE, piloto)
    assert not no_piloto("+5583900000002", piloto)


@pytest.mark.parametrize("telefone", ["", "abc", "+1 202 555 0100"])
def test_o_que_nao_e_telefone_brasileiro_nao_esta_no_piloto(politica, telefone):
    assert not no_piloto(telefone, piloto_com(politica, EQUIPE))


@pytest.mark.parametrize(
    ("guardado", "da_lista"),
    [("+5583999990001", "+558399990001"), ("+558399990001", "+5583999990001")],
)
def test_no_piloto_o_telefone_casa_com_e_sem_o_9(politica, guardado, da_lista):
    """A1: o CTN dá o telefone com o 9 e a lista do piloto pode ter sido escrita sem ele."""
    assert no_piloto(guardado, piloto_com(politica, da_lista))


def _politica_com(tmp_path, liberacao: str):
    texto = (RAIZ / "politica.toml").read_text(encoding="utf-8")
    linha = 'liberacao = { modo = "piloto", telefones = [] }'
    assert linha in texto
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto.replace(linha, liberacao), encoding="utf-8")
    return carregar_politica(caminho)


def test_mudar_o_piloto_e_so_mudar_o_arquivo(tmp_path):
    piloto = _politica_com(tmp_path, f'liberacao = {{ modo = "piloto", telefones = ["{EQUIPE}"] }}')
    assert piloto.liberacao == LiberacaoPiloto(modo="piloto", telefones=frozenset({EQUIPE}))


@pytest.mark.parametrize(
    "liberacao",
    [
        'liberacao = "sombra"',
        'liberacao = { modo = "sombra" }',
        'liberacao = { modo = "carteira" }',
        'liberacao = "piloto"',
    ],
)
def test_liberacao_que_nao_e_o_piloto_e_recusada(tmp_path, liberacao):
    with pytest.raises(PoliticaInvalida, match="liberacao"):
        _politica_com(tmp_path, liberacao)


def test_os_telefones_do_piloto_sao_normalizados_ao_carregar(tmp_path):
    piloto = _politica_com(
        tmp_path, 'liberacao = { modo = "piloto", telefones = ["+55 83 90000-0001"] }'
    )
    assert piloto.liberacao.telefones == frozenset({EQUIPE})


@pytest.mark.parametrize("telefone", ["8390000", "+1 202 555 0100", ""])
def test_telefone_invalido_no_piloto_e_recusado(tmp_path, telefone):
    with pytest.raises(PoliticaInvalida, match="telefones"):
        _politica_com(tmp_path, f'liberacao = {{ modo = "piloto", telefones = ["{telefone}"] }}')


# No turno: quem está fora do piloto não gera turno, nem chamada ao Luna, nem mensagem.


def _deps(politica, llm):
    return Dependencias(
        politica=politica,
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=llm,
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _escrever(sessao, ligada, deps, texto="quanto devo?"):
    registrar_mensagem(sessao, ligada.id, OrigemMensagem.CLIENTE, texto, "cw-1", False, SEGUNDA_10H)
    return processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)


def test_telefone_fora_do_piloto_que_escreve_nao_gera_turno_nem_chamada_ao_llm(
    sessao, ligada, politica
):
    llm = LLMTurnoFalso(respostas=[_bruto()], registrar=True)
    deps = _deps(piloto_com(politica, "+5583999999999"), llm)

    resultado = _escrever(sessao, ligada, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert sessao.scalars(select(Turno)).all() == []
    assert llm.pedidos == []
    assert deps.chatwoot.chamadas == []
    # A rajada foi consumida: a fila não volta a chamar a conversa.
    assert mensagens_pendentes(sessao, ligada.id) == []
    assert ligada.turno_em is None


def test_com_o_piloto_vazio_ninguem_conversa(sessao, ligada, politica):
    llm = LLMTurnoFalso(respostas=[_bruto()], registrar=True)
    deps = _deps(politica, llm)

    resultado = _escrever(sessao, ligada, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert sessao.scalars(select(Turno)).all() == []
    assert llm.pedidos == [] and deps.chatwoot.chamadas == []


def test_telefone_do_piloto_conversa_e_o_turno_e_ativo(sessao, ligada, politica):
    llm = LLMTurnoFalso(respostas=[_bruto()])
    deps = _deps(piloto_com(politica, EQUIPE), llm)

    resultado = _escrever(sessao, ligada, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert sessao.scalars(select(Turno)).one().modo is Modo.ATIVO
    assert len(deps.chatwoot.enviadas(555)) == 1
