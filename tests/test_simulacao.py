"""Conversas simuladas (tarefa 4.6). Com a falsa, todos os casos passam: testa o encanamento."""

import httpx
import pytest

import agente.cli
from agente.cli import main
from agente.integracoes.jev import JevFalso, responder
from agente.integracoes.jev_typesafe import JevTypeSafe
from agente.integracoes.redator import RedatorFalso
from agente.politica import Regua
from agente.simulacao import (
    CasoInvalido,
    DependenciasSimulacao,
    carregar_casos,
    formatar_relatorio,
    rodar_simulacao,
)
from agente.tipos import MotivoHumano

from .conftest import RAIZ
from .integracoes.test_jev_typesafe import handler_ok

CONVERSAS = RAIZ / "tests" / "conversas"


@pytest.fixture
def casos():
    return carregar_casos(CONVERSAS)


def _yaml(tmp_path, texto, nome="caso.yaml"):
    caminho = tmp_path / nome
    caminho.write_text(texto, encoding="utf-8")
    return caminho


CASO_R3 = """
nome: R3 com objeção
regua: R3
mensalidades: 3
propenso: false
turnos:
  - cliente: Tá caro.
    perguntas: {objecao_valor: true, acao: OFFER_DISCOUNT}
    esperado: {acao: OFFER_DISCOUNT, anexos: [SEND_CTN_LINK]}
"""


def test_com_a_falsa_todos_os_roteiros_passam(casos, politica):
    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica))

    assert relatorio.divergencias == []
    assert relatorio.casos == len(casos) == 16
    assert all(a.acertos == a.total for a in relatorio.por_pergunta.values())
    assert all(a.acertos == a.total for a in relatorio.por_acao.values())


def test_os_roteiros_cobrem_o_que_a_tarefa_pede(casos):
    assert {caso.regua for caso in casos} == set(Regua)
    gatilhos = {t.esperado.gatilho for caso in casos for t in caso.turnos}
    assert {
        MotivoHumano.ATRITO,
        MotivoHumano.ENERGISA,
        MotivoHumano.JURIDICO,
        MotivoHumano.CONCILIACAO,
        MotivoHumano.RETENCAO,
        MotivoHumano.OBJECAO_SEM_DESCONTO,
    } <= gatilhos
    objecoes = {
        (caso.regua, sum(bool(t.perguntas.get("objecao_valor")) for t in caso.turnos))
        for caso in casos
    }
    assert {(Regua.R3, 3), (Regua.NR, 2)} <= objecoes


def test_o_degrau_avanca_entre_os_turnos(politica, tmp_path):
    [caso] = carregar_casos(_yaml(tmp_path, CASO_R3))
    tres = caso.model_copy(update={"turnos": caso.turnos * 3})
    relatorio = rodar_simulacao([tres], DependenciasSimulacao(politica))
    assert relatorio.divergencias == []
    # A 4ª objeção na R3 já não tem degrau: fica fora do esperado.
    quatro = caso.model_copy(update={"turnos": caso.turnos * 4})
    [divergencia] = rodar_simulacao([quatro], DependenciasSimulacao(politica)).divergencias
    assert (divergencia.turno, divergencia.obtido) == (4, "HUMAN:OBJECAO_SEM_DESCONTO")


def test_um_jev_que_erra_aparece_nos_acertos_e_nas_divergencias(politica, tmp_path):
    casos = carregar_casos(_yaml(tmp_path, CASO_R3))
    # Um "Jev real" que não vê a objeção: o desconto sai barrado.
    jev = JevFalso([responder("objecao_valor", False), responder("acao", "OFFER_DISCOUNT")])

    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica, jev=jev))

    objecao = relatorio.por_pergunta["objecao_valor"]
    assert (objecao.acertos, objecao.total) == (0, 1)
    assert relatorio.por_pergunta["acao"].acertos == 1
    assert relatorio.por_acao["OFFER_DISCOUNT"].acertos == 0
    [divergencia] = relatorio.divergencias
    assert divergencia.caso == "R3 com objeção"
    assert (divergencia.esperado, divergencia.obtido) == (
        "OFFER_DISCOUNT",
        "HUMAN:BAIXA_CONFIANCA",
    )


def test_anexo_diferente_e_divergencia(politica, tmp_path):
    texto = CASO_R3.replace("anexos: [SEND_CTN_LINK]", "anexos: [GENERATE_PIX]")
    relatorio = rodar_simulacao(
        carregar_casos(_yaml(tmp_path, texto)), DependenciasSimulacao(politica)
    )
    [divergencia] = relatorio.divergencias
    assert divergencia.campo == "anexos"


def test_o_caso_para_na_saida_para_o_humano(politica, tmp_path):
    texto = CASO_R3.replace("objecao_valor: true,", "quer_retencao: true, objecao_valor: true,")
    [caso] = carregar_casos(_yaml(tmp_path, texto))
    dois = caso.model_copy(update={"turnos": caso.turnos * 2})
    relatorio = rodar_simulacao([dois], DependenciasSimulacao(politica))
    campos = [(d.turno, d.campo) for d in relatorio.divergencias]
    assert campos == [(1, "acao"), (2, "caso")]


def test_relatorio_formatado(politica, casos):
    texto = formatar_relatorio(rodar_simulacao(casos, DependenciasSimulacao(politica)))
    assert "16 casos" in texto
    assert "objecao_valor" in texto
    assert "HUMAN:RETENCAO" in texto
    assert "Divergências: nenhuma" in texto


@pytest.mark.parametrize(
    ("trocar", "por", "mensagem"),
    [
        ("objecao_valor: true", "objecao_valr: true", "objecao_valr"),
        ("objecao_valor: true", "objecao_valor: talvez", "objecao_valor"),
        ("acao: OFFER_DISCOUNT}", "acao: PAGAR}", "PAGAR"),
        ("esperado: {acao", "esperado: {gatilho: TOM, acao", "gatilho ou a ação"),
        ("regua: R3", "regua: R9", "regua"),
    ],
)
def test_caso_invalido_diz_o_arquivo_e_o_campo(tmp_path, trocar, por, mensagem):
    caminho = _yaml(tmp_path, CASO_R3.replace(trocar, por))
    with pytest.raises(CasoInvalido, match=mensagem) as erro:
        carregar_casos(caminho)
    assert "caso.yaml" in str(erro.value)


def test_comando_simular(capsys):
    codigo = main(["simular", str(CONVERSAS), "--politica", str(RAIZ / "politica.toml")])
    saida = capsys.readouterr().out
    assert codigo == 0
    assert "16 casos" in saida and "Divergências: nenhuma" in saida


def test_comando_simular_com_divergencia_sai_com_1(tmp_path, capsys):
    caminho = _yaml(tmp_path, CASO_R3.replace("[SEND_CTN_LINK]", "[]"))
    assert main(["simular", str(caminho), "--politica", str(RAIZ / "politica.toml")]) == 1
    assert "anexos" in capsys.readouterr().out


def test_comando_simular_le_a_politica_como_o_config(tmp_path, monkeypatch, capsys):
    # Fora da pasta do repo, vale o AGENTE_POLITICA_PATH, como nos outros comandos.
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(RAIZ / "politica.toml"))
    monkeypatch.chdir(tmp_path)
    assert main(["simular", str(CONVERSAS)]) == 0
    assert "16 casos" in capsys.readouterr().out


def test_simular_jev_real_sem_a_chave_sai_com_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTE_JEV_API_KEY", raising=False)
    caminho = _yaml(tmp_path, CASO_R3)

    codigo = main(
        ["simular", str(caminho), "--politica", str(RAIZ / "politica.toml"), "--jev-real"]
    )

    assert codigo == 2
    assert "AGENTE_JEV_API_KEY" in capsys.readouterr().out


def test_simular_jev_real_nao_exige_a_liberacao_e_imprime_os_tokens(tmp_path, monkeypatch, capsys):
    chamadas = []

    def handler(pedido):
        chamadas.append(pedido)
        return handler_ok(pedido)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTE_JEV_API_KEY", "chave-ficticia")
    monkeypatch.delenv("AGENTE_JEV_DADOS_REAIS_LIBERADOS", raising=False)
    monkeypatch.setattr(
        agente.cli,
        "_abrir_jev",
        lambda config, politica: JevTypeSafe.da_config(
            config, politica.jev_timeout_ms, transporte=httpx.MockTransport(handler)
        ),
    )
    caminho = _yaml(tmp_path, CASO_R3)

    # O Jev do mock responde sempre o neutro: o relatório tem divergências (código 1), e o que
    # importa aqui é que o Jev real foi chamado e os tokens foram somados.
    codigo = main(
        ["simular", str(caminho), "--politica", str(RAIZ / "politica.toml"), "--jev-real"]
    )

    saida = capsys.readouterr().out
    assert codigo == 1
    assert chamadas
    assert f"Tokens de entrada do Jev: {412 * len(chamadas)}" in saida
    assert "Jev falso" not in saida and "typesafe:jev-latest" in saida


def test_erro_de_codigo_no_turno_vira_divergencia_e_nao_um_erro_de_banco(tmp_path, politica):
    # O rollback do erro interno não pode apagar a conversa simulada e esconder o erro real.
    casos = carregar_casos(_yaml(tmp_path, CASO_R3))
    redator = RedatorFalso(falha=KeyError("defeito"))

    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica, redator=redator))

    assert [(d.caso, d.turno, d.campo, d.obtido) for d in relatorio.divergencias] == [
        ("R3 com objeção", 1, "erro", "KeyError")
    ]
