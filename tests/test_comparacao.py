"""A comparação dos redatores (tarefa 5.4), com redatores falsos e o Groq só no respx."""

import json

import httpx
import pytest
import respx

from agente.cli import main
from agente.comparacao import (
    Candidato,
    _Medidor,
    comparar_redatores,
    formatar_comparacao,
    ler_candidato,
)
from agente.integracoes.redator import PedidoRedacao, RedatorFalso, RedatorIndisponivel, Uso
from agente.simulacao import DependenciasSimulacao, carregar_casos

from .conftest import RAIZ

CONVERSAS = RAIZ / "tests" / "conversas"
POLITICA = str(RAIZ / "politica.toml")
PEDIDO = PedidoRedacao(
    acoes=("INFORM_DEBT",),
    marcadores=("divida",),
    tom_alvo="cordial",
    estado="ENGAJADO",
    historico=(),
    nome_cliente="Ana",
    reprovadas_antes=None,
)


@pytest.fixture
def casos():
    return carregar_casos(CONVERSAS)


def test_o_relatorio_sai_com_dois_redatores_falsos(casos, politica):
    bom = Candidato(
        RedatorFalso(nome="bom", uso=Uso(1000, 200), demora_s=0.002),
        preco_entrada=0.15,
        preco_saida=0.75,
    )
    # Escreve o valor cru sempre: a interpolação reprova toda tentativa.
    ruim = Candidato(RedatorFalso(nome="ruim", respostas=["Fica R$ 70."] * 100))

    relatorio = comparar_redatores([bom, ruim], casos, DependenciasSimulacao(politica))

    assert relatorio.casos == len(casos)
    resultado_bom, resultado_ruim = relatorio.resultados
    assert (resultado_bom.nome, resultado_ruim.nome) == ("bom", "ruim")

    assert resultado_bom.tentativas > 0
    assert (resultado_bom.reprovadas, resultado_bom.taxa_reprovacao) == (0, 0.0)
    assert resultado_bom.respostas_seguras == 0 and resultado_bom.divergencias == 0
    assert resultado_bom.tamanho_medio > 0
    assert resultado_bom.latencia_media_s >= 0.002
    chamadas = resultado_bom.tentativas
    assert resultado_bom.tokens_por_conversa == pytest.approx(1200 * chamadas / len(casos))
    custo = (1000 * 0.15 + 200 * 0.75) / 1_000_000 * chamadas / len(casos)
    assert resultado_bom.custo_por_conversa_usd == pytest.approx(custo)

    assert resultado_ruim.taxa_reprovacao == 1.0
    assert resultado_ruim.respostas_seguras > 0
    assert resultado_ruim.tamanho_medio == 0
    assert resultado_ruim.custo_por_conversa_usd == 0
    # Na 2ª reprovação dupla da mesma conversa, ela vai para o humano e o roteiro diverge.
    assert resultado_ruim.divergencias > 0


def test_o_relatorio_formatado_traz_cada_candidato(casos, politica):
    candidatos = [Candidato(RedatorFalso(nome="a")), Candidato(RedatorFalso(nome="b"))]
    texto = formatar_comparacao(
        comparar_redatores(candidatos, casos, DependenciasSimulacao(politica))
    )
    assert "15 casos" in texto
    assert "reprovação" in texto and "latência" in texto and "custo" in texto
    assert "\n  a:" in texto and "\n  b:" in texto


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("openai/gpt-oss-120b", ("openai/gpt-oss-120b", 0.0, 0.0)),
        ("openai/gpt-oss-120b:0.15:0.75", ("openai/gpt-oss-120b", 0.15, 0.75)),
    ],
)
def test_le_o_modelo_e_o_preco_do_candidato(valor, esperado):
    assert ler_candidato(valor) == esperado


@pytest.mark.parametrize("valor", ["m:0.1", "m:caro:0.2", ""])
def test_candidato_invalido_e_recusado(valor):
    with pytest.raises(ValueError):
        ler_candidato(valor)


def test_comando_sem_a_chave_do_groq_sai_com_2(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("AGENTE_GROQ_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # sem o .env do repo
    codigo = main(["comparar-redatores", str(CONVERSAS), "--politica", POLITICA, "--modelo", "m"])
    assert codigo == 2
    assert "AGENTE_GROQ_API_KEY" in capsys.readouterr().out


def _responder_com_os_marcadores(requisicao):
    """Um Groq de mentira que escreve só os marcadores obrigatórios do pedido."""
    pedido = json.loads(requisicao.content)["messages"][1]["content"]
    linha = next(li for li in pedido.splitlines() if li.startswith("Marcadores obrigatórios:"))
    marcadores = linha.split(":", 1)[1].strip()
    texto = "Certo." if marcadores == "nenhum" else f"Oi! {marcadores}"
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": texto}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 50},
        },
    )


@respx.mock
def test_comando_compara_os_modelos_do_groq(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AGENTE_GROQ_API_KEY", "chave-ficticia")
    monkeypatch.chdir(tmp_path)
    rota = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=_responder_com_os_marcadores
    )

    codigo = main(
        [
            "comparar-redatores",
            str(CONVERSAS),
            "--politica",
            POLITICA,
            "--modelo",
            "openai/gpt-oss-120b:0.15:0.75",
            "--modelo",
            "outro-modelo",
        ]
    )

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "groq:openai/gpt-oss-120b" in saida and "groq:outro-modelo" in saida
    modelos = {json.loads(c.request.content)["model"] for c in rota.calls}
    assert modelos == {"openai/gpt-oss-120b", "outro-modelo"}


def test_a_chamada_que_falha_nao_soma_os_tokens_da_anterior():
    medidor = _Medidor(RedatorFalso(uso=Uso(10, 5)))
    medidor.redigir(PEDIDO)
    medidor.redator.falha = RedatorIndisponivel("fora")

    with pytest.raises(RedatorIndisponivel):
        medidor.redigir(PEDIDO)

    assert (medidor.chamadas, medidor.uso) == (2, [Uso(10, 5)])
