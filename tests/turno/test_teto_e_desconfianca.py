"""O teto depois da saída por desconfiança, a volta ao humano de quem já saiu pelo teto e a
desconfiança junto com outro gatilho (tarefa 8.11): nenhum caminho deixa o cliente sem resposta,
e a fixa do golpe (D44) só na saída por desconfiança."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Mensagem, Transicao
from agente.tipos import EstadoConversa, MotivoHumano, OrigemMensagem, ResultadoTurno
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.handoff import NOTA_TETO_REPETIDO, devolver_ao_robo

from .auxiliares import SEGUNDA_10H, Cliente, _bruto, _deps

# O Luna lê os sinais; sem handoff_reply, a mensagem ao cliente é a fixa do catálogo.
DESCONFIA = _bruto(desconfia_golpe=True, acao="NONE", reply="")


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


@pytest.fixture
def cliente(sessao, ligada):
    return Cliente(sessao, ligada)


def _devolver(sessao, conversa, minutos=30):
    devolver_ao_robo(sessao, conversa, SEGUNDA_10H + timedelta(minutes=minutos))
    assert conversa.estado is not EstadoConversa.ATENDIMENTO_HUMANO and not conversa.human_owned


def _cinco_do_agente(sessao, conversa, politica):
    for i in range(politica.teto_resolucao):
        registrar_mensagem(
            sessao,
            conversa.id,
            OrigemMensagem.AGENTE,
            f"r{i}",
            f"a{i}",
            True,
            SEGUNDA_10H + timedelta(minutes=40),
        )


def _transicoes_para_o_humano(sessao):
    return [
        t.motivo
        for t in sessao.scalars(
            select(Transicao)
            .where(Transicao.para == EstadoConversa.ATENDIMENTO_HUMANO)
            .order_by(Transicao.id)
        )
    ]


def test_a_6a_de_quem_desconfiou_e_devolvido_e_a_comum(sessao, ligada, cliente, politica):
    deps = _deps(politica, [DESCONFIA])
    assert cliente("Isso é mesmo do Cartão de Todos?", deps) is ResultadoTurno.HANDOFF
    _devolver(sessao, ligada)
    _cinco_do_agente(sessao, ligada, politica)

    assert cliente("e aí?", deps) is ResultadoTurno.FIXA

    # A saída por desconfiança foi a fixa do golpe; a 6ª é a comum, sem botão, com a transição TETO.
    assert deps.chatwoot.enviadas(555) == [politica.texto_humano_no_whatsapp, TEXTO_CANAL_EXTERNO]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert ligada.motivo_humano is MotivoHumano.TETO
    assert _transicoes_para_o_humano(sessao) == [MotivoHumano.DESCONFIANCA, MotivoHumano.TETO]
    fixas = sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA)).all()
    assert [m.conta_no_teto for m in fixas] == [False, False]


def test_a_6a_comum_sai_sem_botao(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    _cinco_do_agente(sessao, ligada, politica)

    assert cliente("e aí?", deps) is ResultadoTurno.FIXA

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert _transicoes_para_o_humano(sessao) == [MotivoHumano.TETO]


def test_quem_saiu_pelo_teto_e_foi_devolvido_volta_ao_humano_sem_mensagem(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica)
    _cinco_do_agente(sessao, ligada, politica)
    assert cliente("e aí?", deps) is ResultadoTurno.FIXA
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    _devolver(sessao, ligada)
    antes = len(deps.chatwoot.chamadas)

    assert cliente("alguém aí?", deps) is ResultadoTurno.HANDOFF

    novas = deps.chatwoot.chamadas[antes:]
    assert [c[0] for c in novas] == ["nota_privada", "etiquetar", "atribuir", "mudar_status"]
    assert NOTA_TETO_REPETIDO in novas[0][2]
    assert novas[1][2] == ["teto"] and novas[2][2] == 4 and novas[3][2] == "open"
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert ligada.motivo_humano is MotivoHumano.TETO
    assert _transicoes_para_o_humano(sessao) == [MotivoHumano.TETO, MotivoHumano.TETO]


def test_quem_desconfiou_e_saiu_pelo_teto_e_foi_devolvido_tambem_volta_sem_mensagem(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [DESCONFIA])
    cliente("Isso é mesmo do Cartão de Todos?", deps)
    _devolver(sessao, ligada)
    _cinco_do_agente(sessao, ligada, politica)
    cliente("e aí?", deps)
    _devolver(sessao, ligada, 50)

    assert cliente("alguém?", deps) is ResultadoTurno.HANDOFF

    assert len(deps.chatwoot.enviadas(555)) == 2
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO


def test_golpe_com_contestacao_sai_com_o_motivo_da_contestacao_e_o_canal_externo(
    sessao, ligada, cliente, politica
):
    saida = _bruto(contesta_divida=True, desconfia_golpe=True, acao="NONE", reply="")
    deps = _deps(politica, [saida])

    assert cliente("isso é falso, não devo nada", deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert ligada.motivo_humano is MotivoHumano.CONTESTACAO
    etiqueta = next(c[2] for c in deps.chatwoot.chamadas if c[0] == "etiquetar")
    assert etiqueta == ["contestacao"]
