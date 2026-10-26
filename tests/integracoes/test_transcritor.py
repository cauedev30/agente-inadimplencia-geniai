"""O Transcritor: o TranscritorOpenAI (só com respx) e o TranscritorFalso (tarefa 8.3)."""

import httpx
import pytest
import respx

from agente.integracoes.transcritor import (
    Transcricao,
    TranscritorFalso,
    TranscritorOpenAI,
    normalizar_texto,
)

URL = "https://transcricao.invalid/v1"
ROTA = f"{URL}/audio/transcriptions"
MODELO = "gpt-4o-mini-transcribe"
AUDIO = b"ogg-fake"


@pytest.fixture(scope="module")
def openai():
    with TranscritorOpenAI("chave-ficticia", MODELO, url=URL) as transcritor:
        yield transcritor


def _texto(texto="já paguei ontem"):
    return httpx.Response(200, json={"text": texto})


class ContratoTranscritor:
    """O que todo Transcritor cumpre. O real e o falso herdam."""

    def test_transcreve_para_um_texto(self, transcritor):
        transcricao = transcritor.transcrever(AUDIO, "audio/ogg")
        assert transcricao.resultado == "transcrito"
        assert transcricao.texto and transcricao.motivo is None


class TestContratoFalso(ContratoTranscritor):
    @pytest.fixture
    def transcritor(self):
        return TranscritorFalso("já paguei")


class TestContratoOpenAI(ContratoTranscritor):
    @pytest.fixture
    def transcritor(self, openai):
        with respx.mock:
            respx.post(ROTA).mock(return_value=_texto())
            yield openai


@respx.mock
def test_manda_o_arquivo_o_modelo_e_o_idioma_em_multipart(openai):
    rota = respx.post(ROTA).mock(return_value=_texto())

    assert openai.transcrever(AUDIO, "audio/ogg") == Transcricao("transcrito", "já paguei ontem")

    pedido = rota.calls.last.request
    assert pedido.headers["authorization"] == "Bearer chave-ficticia"
    assert pedido.headers["content-type"].startswith("multipart/form-data")
    corpo = pedido.content
    assert b'name="file"; filename="audio.ogg"' in corpo and AUDIO in corpo
    assert b"Content-Type: audio/ogg" in corpo
    assert b'name="model"' in corpo and MODELO.encode() in corpo
    assert b'name="language"' in corpo and b"\r\n\r\npt\r\n" in corpo


@pytest.mark.parametrize(
    ("content_type", "nome"),
    [("audio/mpeg", "audio.mp3"), ("audio/mp4", "audio.m4a"), ("audio/opus", "audio.ogg")],
)
@respx.mock
def test_o_arquivo_leva_a_extensao_do_formato(openai, content_type, nome):
    rota = respx.post(ROTA).mock(return_value=_texto())
    openai.transcrever(AUDIO, content_type)
    assert f'filename="{nome}"'.encode() in rota.calls.last.request.content


@pytest.mark.parametrize(
    ("resposta", "motivo"),
    [
        (httpx.Response(500, json={"error": "fora"}), "status_500"),
        (httpx.Response(429, json={"error": "limite"}), "status_429"),
        (httpx.Response(401, json={"error": "chave"}), "status_401"),
        (httpx.Response(200, json={"text": ""}), "vazio"),
        (httpx.Response(200, json={"text": "   \n "}), "vazio"),
        (httpx.Response(200, json={"outro": "campo"}), "erro"),
        (httpx.Response(200, json={"text": 42}), "erro"),
        (httpx.Response(200, text="não é json"), "erro"),
    ],
)
@respx.mock
def test_o_erro_e_o_texto_vazio_viram_falhou_com_o_motivo(openai, resposta, motivo):
    respx.post(ROTA).mock(return_value=resposta)

    assert openai.transcrever(AUDIO, "audio/ogg") == Transcricao("falhou", None, motivo)


@respx.mock
def test_a_demora_e_a_falha_de_rede_viram_falhou_nunca_excecao(openai):
    rota = respx.post(ROTA)
    rota.mock(side_effect=httpx.ReadTimeout("demorou"))
    assert openai.transcrever(AUDIO, "audio/ogg") == Transcricao("falhou", None, "timeout")
    rota.mock(side_effect=httpx.ConnectError("caiu"))
    assert openai.transcrever(AUDIO, "audio/ogg") == Transcricao("falhou", None, "erro")


@respx.mock
def test_o_motivo_nao_leva_a_chave_nem_o_corpo_da_resposta(openai):
    respx.post(ROTA).mock(
        return_value=httpx.Response(401, text="Bearer chave-ficticia texto-do-erro")
    )

    transcricao = openai.transcrever(AUDIO, "audio/ogg")

    assert "chave-ficticia" not in repr(transcricao) and "texto-do-erro" not in repr(transcricao)


@respx.mock
def test_o_texto_vira_uma_linha_so(openai):
    respx.post(ROTA).mock(return_value=_texto("  oi\n\n  já   paguei\tontem \n"))
    assert openai.transcrever(AUDIO, "audio/ogg").texto == "oi já paguei ontem"


@respx.mock
def test_o_texto_longo_e_cortado_em_3000_terminando_em_reticencias(openai):
    respx.post(ROTA).mock(return_value=_texto("a" * 5000))

    texto = openai.transcrever(AUDIO, "audio/ogg").texto

    assert len(texto) == 3000 and texto.endswith("…") and texto[:-1] == "a" * 2999


def test_o_limite_do_corte_e_configuravel():
    assert normalizar_texto("abcdefghij", 5) == "abcd…"
    assert normalizar_texto("abcde", 5) == "abcde"


@respx.mock
def test_o_texto_transcrito_nao_aparece_em_nenhum_log(openai, caplog):
    caplog.set_level("DEBUG")
    respx.post(ROTA).mock(return_value=_texto("palavras-faladas-pelo-cliente"))
    openai.transcrever(AUDIO, "audio/ogg")
    respx.post(ROTA).mock(return_value=httpx.Response(500, text="palavras-faladas-pelo-cliente"))
    openai.transcrever(AUDIO, "audio/ogg")

    assert "palavras-faladas" not in caplog.text


def test_a_falsa_devolve_o_texto_dado():
    assert TranscritorFalso("oi").transcrever(AUDIO, "audio/ogg") == Transcricao("transcrito", "oi")


def test_a_falsa_pode_falhar_com_o_motivo_dado():
    falsa = TranscritorFalso(motivo="timeout")
    assert falsa.transcrever(AUDIO, "audio/ogg") == Transcricao("falhou", None, "timeout")


def test_a_falsa_responde_na_ordem_e_guarda_so_o_tamanho_e_o_tipo():
    falsa = TranscritorFalso(respostas=["um", Transcricao("falhou", None, "erro")], texto="resto")

    assert falsa.transcrever(AUDIO, "audio/ogg").texto == "um"
    assert falsa.transcrever(AUDIO, "audio/mpeg").resultado == "falhou"
    assert falsa.transcrever(AUDIO, "audio/ogg").texto == "resto"
    assert falsa.chamadas == [
        (len(AUDIO), "audio/ogg"),
        (len(AUDIO), "audio/mpeg"),
        (8, "audio/ogg"),
    ]
