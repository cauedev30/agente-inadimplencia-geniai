"""O Redator: o RedatorGroq pela API compatível com a OpenAI (só com respx) e o RedatorFalso."""

import json

import httpx
import pytest
import respx

from agente.integracoes.redator import (
    PedidoRedacao,
    RedatorFalso,
    RedatorGroq,
    RedatorIndisponivel,
    Uso,
    montar_mensagens,
)

URL = "https://groq.invalid/openai/v1"
MODELO = "openai/gpt-oss-120b"

PEDIDO = PedidoRedacao(
    acoes=("INFORM_DEBT", "SEND_CTN_LINK"),
    marcadores=("divida", "link_ctn"),
    tom_alvo="cordial",
    estado="ENGAJADO",
    historico=(("ABERTURA", "[abertura]"), ("CLIENTE", "quanto eu devo?")),
    nome_cliente="Ana",
    reprovadas_antes=None,
    marcadores_opcionais=("mensalidades",),
)


def _resposta(conteudo="Oi, Ana! O valor é {divida}: {link_ctn}", uso=(120, 30)):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": conteudo}}],
            "usage": {"prompt_tokens": uso[0], "completion_tokens": uso[1]},
        },
    )


@pytest.fixture(scope="module")
def groq():
    with RedatorGroq("chave-ficticia", MODELO, url=URL) as redator:
        yield redator


class ContratoRedator:
    """O que todo Redator cumpre. A real e a falsa herdam."""

    def test_devolve_texto_com_os_marcadores(self, redator):
        texto = redator.redigir(PEDIDO)
        assert isinstance(texto, str) and texto.strip()
        assert "{divida}" in texto and "{link_ctn}" in texto

    def test_se_identifica_para_a_comparacao(self, redator):
        assert redator.nome


class TestContratoFalsa(ContratoRedator):
    @pytest.fixture
    def redator(self):
        return RedatorFalso()


class TestContratoGroq(ContratoRedator):
    @pytest.fixture
    def redator(self, groq):
        with respx.mock:
            respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())
            yield groq


@respx.mock
def test_groq_manda_o_modelo_a_chave_e_as_mensagens(groq):
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())

    assert groq.redigir(PEDIDO) == "Oi, Ana! O valor é {divida}: {link_ctn}"

    pedido = rota.calls.last.request
    assert pedido.headers["authorization"] == "Bearer chave-ficticia"
    corpo = json.loads(pedido.content)
    assert corpo["model"] == MODELO
    assert corpo["messages"] == montar_mensagens(PEDIDO)
    assert groq.ultimo_uso == Uso(120, 30)
    assert groq.nome == f"groq:{MODELO}"


@respx.mock
def test_groq_tira_os_espacos_das_pontas(groq):
    respx.post(f"{URL}/chat/completions").mock(return_value=_resposta("\n  Oi, {divida}  \n"))
    assert groq.redigir(PEDIDO) == "Oi, {divida}"


@pytest.mark.parametrize(
    "resposta",
    [
        httpx.Response(500, json={"error": "fora"}),
        httpx.Response(429, json={"error": "limite"}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
        httpx.Response(200, text="não é json"),
    ],
)
@respx.mock
def test_groq_com_erro_fica_indisponivel(groq, resposta):
    respx.post(f"{URL}/chat/completions").mock(return_value=resposta)
    with pytest.raises(RedatorIndisponivel):
        groq.redigir(PEDIDO)


@respx.mock
def test_groq_que_demora_fica_indisponivel(groq):
    respx.post(f"{URL}/chat/completions").mock(side_effect=httpx.ReadTimeout("demorou"))
    with pytest.raises(RedatorIndisponivel, match="demorou"):
        groq.redigir(PEDIDO)


@respx.mock
def test_o_erro_nao_mostra_a_chave(groq):
    respx.post(f"{URL}/chat/completions").mock(return_value=httpx.Response(401))
    with pytest.raises(RedatorIndisponivel) as erro:
        groq.redigir(PEDIDO)
    assert "chave-ficticia" not in str(erro.value)


def test_as_mensagens_levam_as_regras_os_marcadores_e_o_historico():
    pedido = PedidoRedacao(
        **{
            **PEDIDO.__dict__,
            "estado": "EM_RISCO",
            "reprovadas_antes": ("tom_adequado", "marcador_faltando:link_ctn"),
            "objecao_sem_desconto": True,
        }
    )

    sistema, usuario = montar_mensagens(pedido)

    assert sistema["role"] == "system" and usuario["role"] == "user"
    assert "R$" in sistema["content"] and "marcador" in sistema["content"]
    texto = usuario["content"]
    assert "{divida}" in texto and "{link_ctn}" in texto and "{mensalidades}" in texto
    assert "Ana" in texto and "cordial" in texto
    assert "CLIENTE: quanto eu devo?" in texto
    assert "tom_adequado" in texto and "marcador_faltando:link_ctn" in texto
    assert "não tem desconto" in texto
    assert "incomodado" in texto


def test_a_falsa_devolve_as_respostas_dadas_e_depois_o_modelo():
    falsa = RedatorFalso(respostas=["R$ 70 cru"])
    assert falsa.redigir(PEDIDO) == "R$ 70 cru"
    assert "{link_ctn}" in falsa.redigir(PEDIDO)


def test_a_falsa_pode_falhar_e_informar_o_uso():
    falsa = RedatorFalso(uso=Uso(10, 5))
    falsa.redigir(PEDIDO)
    assert falsa.ultimo_uso == Uso(10, 5)
    with pytest.raises(RedatorIndisponivel):
        RedatorFalso(falha=RedatorIndisponivel("fora")).redigir(PEDIDO)


def test_a_falsa_so_guarda_os_pedidos_quando_o_teste_liga():
    # O pedido traz o histórico do cliente: em produção, a falsa não acumula dado pessoal.
    padrao = RedatorFalso()
    padrao.redigir(PEDIDO)
    assert padrao.pedidos == []
    registrando = RedatorFalso(registrar=True)
    registrando.redigir(PEDIDO)
    assert registrando.pedidos == [PEDIDO]


def test_a_falsa_zera_o_uso_da_chamada_que_falha():
    falsa = RedatorFalso(uso=Uso(10, 5))
    falsa.redigir(PEDIDO)
    falsa.falha = RedatorIndisponivel("fora")

    with pytest.raises(RedatorIndisponivel):
        falsa.redigir(PEDIDO)

    assert falsa.ultimo_uso is None


@respx.mock
def test_o_groq_zera_o_uso_da_chamada_que_falha(groq):
    rota = respx.post(f"{URL}/chat/completions")
    rota.mock(return_value=_resposta())
    groq.redigir(PEDIDO)
    assert groq.ultimo_uso == Uso(120, 30)

    rota.mock(return_value=httpx.Response(503))
    with pytest.raises(RedatorIndisponivel):
        groq.redigir(PEDIDO)

    assert groq.ultimo_uso is None
