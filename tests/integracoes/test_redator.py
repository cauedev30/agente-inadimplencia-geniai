"""O Redator: o RedatorOpenAI pela API da OpenAI (só com respx) e o RedatorFalso."""

import json

import httpx
import pytest
import respx

from agente.integracoes.redator import (
    URL_OPENAI,
    PedidoRedacao,
    RedatorFalso,
    RedatorIndisponivel,
    RedatorOpenAI,
    Uso,
    montar_mensagens,
)

URL = "https://openai.invalid/v1"
MODELO = "gpt-6-luna"

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
def openai():
    with RedatorOpenAI("chave-ficticia", MODELO, url=URL) as redator:
        yield redator


class ContratoRedator:
    """O que todo Redator cumpre. A real e a falsa herdam."""

    def test_devolve_texto_com_os_marcadores(self, redator):
        texto = redator.redigir(PEDIDO)
        assert isinstance(texto, str) and texto.strip()
        assert "{divida}" in texto and "{link_ctn}" in texto

    def test_se_identifica(self, redator):
        assert redator.nome


class TestContratoFalsa(ContratoRedator):
    @pytest.fixture
    def redator(self):
        return RedatorFalso()


class TestContratoOpenAI(ContratoRedator):
    @pytest.fixture
    def redator(self, openai):
        with respx.mock:
            respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())
            yield openai


@respx.mock
def test_openai_manda_o_modelo_a_chave_e_as_mensagens(openai):
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())

    assert openai.redigir(PEDIDO) == "Oi, Ana! O valor é {divida}: {link_ctn}"

    pedido = rota.calls.last.request
    assert pedido.headers["authorization"] == "Bearer chave-ficticia"
    corpo = json.loads(pedido.content)
    assert corpo["model"] == MODELO
    assert corpo["messages"] == montar_mensagens(PEDIDO)
    assert openai.ultimo_uso == Uso(120, 30)
    assert openai.nome == f"openai:{MODELO}"


@respx.mock
def test_openai_tira_os_espacos_das_pontas(openai):
    respx.post(f"{URL}/chat/completions").mock(return_value=_resposta("\n  Oi, {divida}  \n"))
    assert openai.redigir(PEDIDO) == "Oi, {divida}"


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
def test_openai_com_erro_fica_indisponivel(openai, resposta):
    respx.post(f"{URL}/chat/completions").mock(return_value=resposta)
    with pytest.raises(RedatorIndisponivel):
        openai.redigir(PEDIDO)


@respx.mock
def test_openai_que_demora_fica_indisponivel(openai):
    respx.post(f"{URL}/chat/completions").mock(side_effect=httpx.ReadTimeout("demorou"))
    with pytest.raises(RedatorIndisponivel, match="demorou"):
        openai.redigir(PEDIDO)


@respx.mock
def test_o_erro_nao_mostra_a_chave(openai):
    respx.post(f"{URL}/chat/completions").mock(return_value=httpx.Response(401))
    with pytest.raises(RedatorIndisponivel) as erro:
        openai.redigir(PEDIDO)
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
def test_o_openai_zera_o_uso_da_chamada_que_falha(openai):
    rota = respx.post(f"{URL}/chat/completions")
    rota.mock(return_value=_resposta())
    openai.redigir(PEDIDO)
    assert openai.ultimo_uso == Uso(120, 30)

    rota.mock(return_value=httpx.Response(503))
    with pytest.raises(RedatorIndisponivel):
        openai.redigir(PEDIDO)

    assert openai.ultimo_uso is None


@respx.mock
def test_o_corpo_leva_o_reasoning_effort_e_a_temperatura():
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())
    corpo_extra = {"reasoning_effort": "none"}
    with RedatorOpenAI("chave-ficticia", MODELO, url=URL, corpo_extra=corpo_extra) as luna:
        luna.redigir(PEDIDO)

    corpo = json.loads(rota.calls.last.request.content)
    assert corpo["reasoning_effort"] == "none"
    assert corpo["temperature"] == 0.3
    assert corpo["model"] == MODELO


@respx.mock
def test_sem_corpo_extra_o_corpo_nao_leva_o_reasoning_effort(openai):
    rota = respx.post(f"{URL}/chat/completions").mock(return_value=_resposta())
    openai.redigir(PEDIDO)
    assert "reasoning_effort" not in json.loads(rota.calls.last.request.content)


@respx.mock
def test_o_http_400_da_temperatura_fica_indisponivel_sem_a_chave(openai):
    # Sem o reasoning_effort "none", o Luna recusa a temperatura com 400 (medido no outro agente).
    erro = {"error": {"message": "temperature não suportada; Bearer chave-ficticia"}}
    respx.post(f"{URL}/chat/completions").mock(return_value=httpx.Response(400, json=erro))

    with pytest.raises(RedatorIndisponivel, match="400") as falha:
        openai.redigir(PEDIDO)

    assert "chave-ficticia" not in str(falha.value)


def test_a_url_padrao_e_a_da_openai():
    assert URL_OPENAI == "https://api.openai.com/v1"
    with RedatorOpenAI("chave-ficticia", MODELO) as luna:
        assert str(luna._cliente.base_url).rstrip("/") == URL_OPENAI


def test_as_instrucoes_explicam_o_rotulo_do_audio_transcrito():
    # O áudio do cliente chega ao histórico como texto, com um rótulo (D43, tarefa 8.4).
    [sistema, _] = montar_mensagens(PEDIDO)

    instrucoes = " ".join(sistema["content"].split())
    assert "[áudio transcrito:" in instrucoes
    assert "pode ter erro de transcrição" in instrucoes
    assert "[áudio longo demais" in instrucoes and "[áudio — não foi possível ouvir]" in instrucoes


def test_as_instrucoes_proibem_fato_do_produto_sem_fonte_e_promessa_fora_das_acoes():
    # O redator não afirma nada do produto e não promete o que o agente não faz (tarefa 8.8).
    [sistema, _] = montar_mensagens(PEDIDO)

    instrucoes = " ".join(sistema["content"].split())
    assert "Não afirme nada sobre a rede, as clínicas, os serviços ou as coberturas" in instrucoes
    assert "Não afirme nada sobre o Cartão de Todos" not in instrucoes
    assert "o assunto não é tratado por aqui" in instrucoes
    assert (
        "Se o cliente desconfiar de golpe, acolha a preocupação sem negar nem pôr em dúvida quem "
        "está falando, e siga com as ações do turno"
    ) in instrucoes
    assert (
        "Não prometa verificar, retornar, avisar nem fazer qualquer coisa que não esteja nas ações"
        in instrucoes
    )


def test_as_instrucoes_dizem_que_nao_ha_parcelamento():
    """A12 (D23): sem isto o Luna só acerta o parcelamento por sorte e o Jev 3 reprova o resto."""
    pedido = PedidoRedacao(
        acoes=("INFORM_DEBT",),
        marcadores=("divida",),
        tom_alvo="neutro",
        estado="INICIADO",
        historico=(("CLIENTE", "Posso parcelar?"),),
        nome_cliente="Ana",
    )
    sistema = montar_mensagens(pedido)[0]["content"]
    assert "não existe parcelamento" in sistema
    assert "não é possível parcelar por aqui" in sistema
