"""A chamada JSON ao Luna do turno de uma chamada (D47, E1.1): a real (só com respx) e a falsa."""

import json

import httpx
import pytest
import respx

from agente.integracoes.llm_turno import (
    URL_OPENAI,
    JSONInvalido,
    LLMTurnoFalso,
    LLMTurnoIndisponivel,
    LLMTurnoOpenAI,
    Uso,
)

URL = "https://openai.invalid/v1"
MODELO = "gpt-6-luna"
CHAVE = "chave-ficticia"


def _resposta(conteudo='{"acao": "NONE"}', uso=(120, 30)):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": conteudo}}],
            "usage": {"prompt_tokens": uso[0], "completion_tokens": uso[1]},
        },
    )


@pytest.fixture(scope="module")
def luna():
    with LLMTurnoOpenAI(CHAVE, MODELO, url=URL) as llm:
        yield llm


@respx.mock
def test_o_corpo_leva_o_response_format_o_modelo_e_a_temperatura(luna):
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())

    assert luna.completar_json("sistema", "usuario") == {"acao": "NONE"}

    pedido = rota.calls.last.request
    assert pedido.headers["authorization"] == f"Bearer {CHAVE}"
    corpo = json.loads(pedido.content)
    assert corpo["response_format"] == {"type": "json_object"}
    assert corpo["model"] == MODELO
    assert corpo["temperature"] == 0.3
    assert corpo["messages"] == [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "usuario"},
    ]
    assert luna.nome == f"openai:{MODELO}"


@respx.mock
def test_o_corpo_extra_entra_no_pedido():
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())
    with LLMTurnoOpenAI(
        CHAVE, MODELO, url=URL, corpo_extra={"reasoning_effort": "none"}, temperatura=0.1
    ) as llm:
        llm.completar_json("s", "u")

    corpo = json.loads(rota.calls.last.request.content)
    assert corpo["reasoning_effort"] == "none"
    assert corpo["temperature"] == 0.1


@respx.mock
def test_objeto_json_vira_dict_e_o_cercado_tambem(luna):
    rota = respx.post(f"{URL}/chat/completions")
    rota.mock(return_value=_resposta('  {"a": 1, "b": "é"}  '))
    assert luna.completar_json("s", "u") == {"a": 1, "b": "é"}

    rota.mock(return_value=_resposta('```json\n{"a": 2}\n```'))
    assert luna.completar_json("s", "u") == {"a": 2}

    rota.mock(return_value=_resposta('```\n{"a": 3}\n```'))
    assert luna.completar_json("s", "u") == {"a": 3}

    rota.mock(return_value=_resposta('Aqui está:\n```json\n{"a": 4}\n```\nPronto.'))
    assert luna.completar_json("s", "u") == {"a": 4}


@pytest.mark.parametrize("conteudo", ["oi, tudo bem?", "[1, 2]", '"texto"', "{quebrado", "3"])
@respx.mock
def test_texto_solto_e_lista_levantam_json_invalido(luna, conteudo):
    respx.post(f"{URL}/chat/completions").mock(return_value=_resposta(conteudo))
    with pytest.raises(JSONInvalido):
        luna.completar_json("s", "u")


@pytest.mark.parametrize("status", [401, 500])
@respx.mock
def test_erro_http_diz_so_o_status_sem_a_chave_nem_o_corpo(luna, status):
    corpo = {"error": f"eco do pedido com Bearer {CHAVE} e o historico do cliente"}
    respx.post(f"{URL}/chat/completions").mock(return_value=httpx.Response(status, json=corpo))

    with pytest.raises(LLMTurnoIndisponivel) as erro:
        luna.completar_json("s", "u")

    assert str(status) in str(erro.value)
    assert CHAVE not in str(erro.value)
    assert "historico" not in str(erro.value)


@respx.mock
def test_prazo_estourado_levanta_indisponivel(luna):
    respx.post(f"{URL}/chat/completions").mock(side_effect=httpx.ReadTimeout("demorou"))
    with pytest.raises(LLMTurnoIndisponivel, match="demorou"):
        luna.completar_json("s", "u")


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, text="não é json"),
    ],
)
@respx.mock
def test_conteudo_vazio_ou_resposta_torta_levanta_indisponivel(luna, resposta):
    respx.post(f"{URL}/chat/completions").mock(return_value=resposta)
    with pytest.raises(LLMTurnoIndisponivel):
        luna.completar_json("s", "u")


@respx.mock
def test_ultimo_uso_traz_os_tokens_e_zera_quando_a_chamada_falha(luna):
    rota = respx.post(f"{URL}/chat/completions")
    rota.mock(return_value=_resposta())
    luna.completar_json("s", "u")
    assert luna.ultimo_uso == Uso(120, 30)

    rota.mock(return_value=httpx.Response(503))
    with pytest.raises(LLMTurnoIndisponivel):
        luna.completar_json("s", "u")
    assert luna.ultimo_uso is None


@respx.mock
def test_ultimo_uso_vale_tambem_quando_o_json_e_invalido(luna):
    # Os tokens foram gastos, então a comparação de custo os conta (D47, M7 da revisão da E1).
    rota = respx.post(f"{URL}/chat/completions")
    rota.mock(return_value=_resposta())
    luna.completar_json("s", "u")

    rota.mock(return_value=_resposta("texto solto", uso=(7, 3)))
    with pytest.raises(JSONInvalido):
        luna.completar_json("s", "u")
    assert luna.ultimo_uso == Uso(7, 3)


def test_a_url_padrao_e_a_da_openai():
    with LLMTurnoOpenAI(CHAVE, MODELO) as llm:
        assert str(llm._cliente.base_url).rstrip("/") == URL_OPENAI


def test_a_falsa_devolve_na_ordem_e_levanta_a_excecao_dada():
    falsa = LLMTurnoFalso(
        respostas=[{"n": 1}, '{"n": 2}', LLMTurnoIndisponivel("fora"), "texto solto", {"n": 5}]
    )
    assert falsa.completar_json("s", "u") == {"n": 1}
    assert falsa.completar_json("s", "u") == {"n": 2}
    with pytest.raises(LLMTurnoIndisponivel, match="fora"):
        falsa.completar_json("s", "u")
    with pytest.raises(JSONInvalido):
        falsa.completar_json("s", "u")
    assert falsa.completar_json("s", "u") == {"n": 5}


def test_a_falsa_com_json_que_nao_e_objeto_levanta_json_invalido():
    with pytest.raises(JSONInvalido):
        LLMTurnoFalso(respostas=["[1, 2]"]).completar_json("s", "u")


def test_a_falsa_levanta_quando_as_respostas_acabam():
    falsa = LLMTurnoFalso(respostas=[{"n": 1}])
    falsa.completar_json("s", "u")
    with pytest.raises(LLMTurnoIndisponivel, match="sem resposta na falsa"):
        falsa.completar_json("s", "u")
    with pytest.raises(LLMTurnoIndisponivel, match="sem resposta na falsa"):
        LLMTurnoFalso().completar_json("s", "u")


def test_a_falsa_so_guarda_os_pedidos_quando_o_teste_liga():
    # O pedido traz o histórico do cliente: em produção, a falsa não acumula dado pessoal.
    padrao = LLMTurnoFalso(respostas=[{}])
    padrao.completar_json("s", "u")
    assert padrao.pedidos == []
    registrando = LLMTurnoFalso(respostas=[{}], registrar=True)
    registrando.completar_json("sistema", "usuario")
    assert registrando.pedidos == [("sistema", "usuario")]


def test_a_falsa_informa_o_uso_e_zera_na_chamada_que_falha():
    falsa = LLMTurnoFalso(respostas=[{}, LLMTurnoIndisponivel("fora")], uso=Uso(10, 5))
    falsa.completar_json("s", "u")
    assert falsa.ultimo_uso == Uso(10, 5)
    with pytest.raises(LLMTurnoIndisponivel):
        falsa.completar_json("s", "u")
    assert falsa.ultimo_uso is None
    assert falsa.nome == "falso"
