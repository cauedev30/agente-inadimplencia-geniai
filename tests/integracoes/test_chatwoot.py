import json

import httpx
import pytest
import respx

from agente.integracoes.chatwoot import (
    AnexoBaixado,
    ChatwootFalso,
    ChatwootHttp,
    FalhaDownload,
)
from agente.tipos import Botao

URL = "https://chatwoot.invalid"
BASE = f"{URL}/api/v1/accounts/7/conversations/42"


@pytest.fixture(scope="module")
def chatwoot():
    # Um cliente só: o httpx demora para carregar o SSL a cada cliente novo.
    with ChatwootHttp(URL, conta=7, token="segredo") as cliente:
        yield cliente


def _corpo(rota):
    return json.loads(rota.calls.last.request.content)


@respx.mock
def test_envia_mensagem_e_devolve_o_id(chatwoot):
    rota = respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"id": 991}))

    assert chatwoot.enviar_mensagem(42, "Olá") == "991"
    assert rota.calls.last.request.headers["api_access_token"] == "segredo"
    assert _corpo(rota) == {"content": "Olá", "message_type": "outgoing", "private": False}


@respx.mock
def test_mensagem_com_botoes_de_resposta(chatwoot):
    rota = respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"id": 1}))

    chatwoot.enviar_mensagem(42, "Como prefere pagar?", [Botao("resposta", "Pagar com Pix", "PIX")])

    corpo = _corpo(rota)
    assert corpo["content_type"] == "input_select"
    assert corpo["content_attributes"] == {"items": [{"title": "Pagar com Pix", "value": "PIX"}]}


@respx.mock
def test_botao_de_link_vai_no_texto(chatwoot):
    rota = respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"id": 1}))

    chatwoot.enviar_mensagem(
        42, "Fale com a gente.", [Botao("url", "Falar com um atendente", "https://a.invalid/x")]
    )

    assert (
        _corpo(rota)["content"]
        == "Fale com a gente.\n\nFalar com um atendente: https://a.invalid/x"
    )


@respx.mock
def test_nota_privada(chatwoot):
    rota = respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={"id": 1}))

    chatwoot.nota_privada(42, "laudo do Jev")

    assert _corpo(rota) == {"content": "laudo do Jev", "message_type": "outgoing", "private": True}


@respx.mock
def test_etiquetar_soma_as_etiquetas_que_ja_existem(chatwoot):
    respx.get(f"{BASE}/labels").mock(
        return_value=httpx.Response(200, json={"payload": ["cobranca"]})
    )
    rota = respx.post(f"{BASE}/labels").mock(return_value=httpx.Response(200, json={}))

    chatwoot.etiquetar(42, ["juridico"])

    assert _corpo(rota) == {"labels": ["cobranca", "juridico"]}


@respx.mock
def test_atribuir_status_e_atributos(chatwoot):
    atribuir = respx.post(f"{BASE}/assignments").mock(return_value=httpx.Response(200, json={}))
    status = respx.post(f"{BASE}/toggle_status").mock(return_value=httpx.Response(200, json={}))
    atributos = respx.post(f"{BASE}/custom_attributes").mock(
        return_value=httpx.Response(200, json={})
    )

    chatwoot.atribuir(42, time_id=3)
    chatwoot.mudar_status(42, "open")
    chatwoot.gravar_atributos(42, {"cobranca_id": 10})

    assert _corpo(atribuir) == {"team_id": 3}
    assert _corpo(status) == {"status": "open"}
    assert _corpo(atributos) == {"custom_attributes": {"cobranca_id": 10}}


@respx.mock
def test_erro_da_api_vira_excecao(chatwoot):
    respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        chatwoot.enviar_mensagem(42, "Olá")


def test_status_invalido_e_recusado(chatwoot):
    with pytest.raises(ValueError, match="status"):
        chatwoot.mudar_status(42, "talvez")


def test_falsa_grava_as_chamadas_em_ordem():
    falsa = ChatwootFalso()

    primeiro = falsa.enviar_mensagem(42, "Olá")
    falsa.nota_privada(42, "laudo")
    falsa.etiquetar(42, ["tom"])

    assert primeiro == "falso-1"
    assert [chamada[0] for chamada in falsa.chamadas] == [
        "enviar_mensagem",
        "nota_privada",
        "etiquetar",
    ]
    assert falsa.enviadas(42) == ["Olá"]


@respx.mock
def test_mensagens_do_agente_ficam_so_com_as_que_o_cliente_recebeu(chatwoot):
    rota = respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "payload": [
                    {"id": 1, "content": "Oi, quanto devo?", "message_type": 0, "private": False},
                    {
                        "id": 2,
                        "content": "O valor é R$ 100,20",
                        "message_type": 1,
                        "private": False,
                    },
                    {"id": 3, "content": "laudo do Jev", "message_type": 1, "private": True},
                    {"id": 4, "content": "Atribuída ao time", "message_type": 2, "private": False},
                    {"id": 5, "content": None, "message_type": 1, "private": False},
                ]
            },
        )
    )

    assert chatwoot.mensagens_do_agente(42) == [("2", "O valor é R$ 100,20"), ("5", "")]
    assert rota.calls.last.request.headers["api_access_token"] == "segredo"


@respx.mock
def test_erro_ao_listar_as_mensagens_vira_excecao(chatwoot):
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        chatwoot.mensagens_do_agente(42)


def test_falsa_lista_as_mensagens_do_agente_com_o_id_que_devolveu():
    falsa = ChatwootFalso()
    falsa.enviar_mensagem(42, "Olá")
    falsa.nota_privada(42, "laudo")
    id_outra = falsa.enviar_mensagem(43, "Outra conversa")
    id_segunda = falsa.enviar_mensagem(42, "Tudo bem?")

    assert falsa.mensagens_do_agente(42) == [("falso-1", "Olá"), (id_segunda, "Tudo bem?")]
    assert falsa.mensagens_do_agente(43) == [(id_outra, "Outra conversa")]


# Os anexos (tarefa 8.2): o download do áudio do cliente.
AUDIO = f"{URL}/rails/active_storage/blobs/abc/audio.ogg"
ARMAZENAMENTO = "https://armazenamento.invalid/audio.ogg"
ACEITOS = frozenset({"audio/ogg"})
LIMITE = 1000


def _baixar(chatwoot, url=AUDIO, *, aceitos=ACEITOS, max_bytes=LIMITE, prazo_s=15.0):
    return chatwoot.baixar_anexo(url, aceitos=aceitos, max_bytes=max_bytes, prazo_s=prazo_s)


def _audio(conteudo=b"ogg-fake", tipo="audio/ogg"):
    return httpx.Response(200, content=conteudo, headers={"content-type": tipo})


@respx.mock
def test_baixa_o_anexo_da_origem_do_chatwoot_com_o_token(chatwoot):
    rota = respx.get(AUDIO).mock(return_value=_audio(tipo="audio/ogg; codecs=opus"))

    assert _baixar(chatwoot) == AnexoBaixado(b"ogg-fake", "audio/ogg")
    assert rota.calls.last.request.headers["api_access_token"] == "segredo"


@respx.mock
def test_link_de_outra_origem_nao_e_baixado_nem_recebe_o_token(chatwoot):
    rota = respx.get(ARMAZENAMENTO).mock(return_value=_audio())

    assert _baixar(chatwoot, ARMAZENAMENTO) == FalhaDownload("host")
    assert not rota.called


@pytest.mark.parametrize("url", ["", "ftp://chatwoot.invalid/a", "/rails/a.ogg", f"{URL}:99/a"])
def test_link_que_nao_e_o_do_chatwoot_e_recusado(chatwoot, url):
    assert _baixar(chatwoot, url) == FalhaDownload("host")


@respx.mock
def test_redirecionamento_para_outra_origem_nao_leva_o_token(chatwoot):
    respx.get(AUDIO).mock(return_value=httpx.Response(302, headers={"location": ARMAZENAMENTO}))
    destino = respx.get(ARMAZENAMENTO).mock(return_value=_audio())

    assert _baixar(chatwoot) == AnexoBaixado(b"ogg-fake", "audio/ogg")
    assert "api_access_token" not in destino.calls.last.request.headers


@respx.mock
def test_redirecionamento_relativo_dentro_do_chatwoot_leva_o_token(chatwoot):
    respx.get(AUDIO).mock(return_value=httpx.Response(302, headers={"location": "/outro.ogg"}))
    destino = respx.get(f"{URL}/outro.ogg").mock(return_value=_audio())

    assert isinstance(_baixar(chatwoot), AnexoBaixado)
    assert destino.calls.last.request.headers["api_access_token"] == "segredo"


@respx.mock
def test_no_maximo_3_redirecionamentos(chatwoot):
    respx.get(AUDIO).mock(return_value=httpx.Response(302, headers={"location": AUDIO}))

    assert _baixar(chatwoot) == FalhaDownload("redirects")
    assert len(respx.calls) == 4  # o pedido e os 3 redirecionamentos


@respx.mock
def test_nunca_de_https_para_http(chatwoot):
    respx.get(AUDIO).mock(
        return_value=httpx.Response(302, headers={"location": "http://armazenamento.invalid/a"})
    )
    destino = respx.get("http://armazenamento.invalid/a").mock(return_value=_audio())

    assert _baixar(chatwoot) == FalhaDownload("host")
    assert not destino.called


@respx.mock
def test_tipo_que_nao_e_audio_aceito_e_recusado(chatwoot):
    respx.get(AUDIO).mock(return_value=_audio(tipo="text/html"))
    assert _baixar(chatwoot) == FalhaDownload("type", "text/html", 8)


@respx.mock
def test_tamanho_declarado_acima_do_limite_e_recusado(chatwoot):
    respx.get(AUDIO).mock(return_value=_audio(conteudo=b"x" * (LIMITE + 1)))
    assert _baixar(chatwoot) == FalhaDownload("size", "audio/ogg", LIMITE + 1)


@respx.mock
def test_corpo_sem_tamanho_e_cortado_ao_passar_do_limite(chatwoot):
    pedacos = iter([b"x" * 400] * 10)  # sem Content-Length: lido em pedaços
    respx.get(AUDIO).mock(
        return_value=httpx.Response(200, content=pedacos, headers={"content-type": "audio/ogg"})
    )

    assert _baixar(chatwoot) == FalhaDownload("size", "audio/ogg")


@respx.mock
def test_arquivo_vazio_e_falha(chatwoot):
    respx.get(AUDIO).mock(return_value=_audio(conteudo=b""))
    assert _baixar(chatwoot) == FalhaDownload("empty", "audio/ogg", 0)


@respx.mock
def test_status_de_erro_e_falha(chatwoot):
    respx.get(AUDIO).mock(return_value=httpx.Response(404))
    assert _baixar(chatwoot) == FalhaDownload("status")


@respx.mock
def test_demora_e_falha_de_rede_viram_resultado_nunca_excecao(chatwoot):
    rota = respx.get(AUDIO)
    rota.mock(side_effect=httpx.ReadTimeout("demorou"))
    assert _baixar(chatwoot) == FalhaDownload("timeout")
    rota.mock(side_effect=httpx.ConnectError("caiu"))
    assert _baixar(chatwoot) == FalhaDownload("error")


@respx.mock
def test_a_falha_nao_leva_o_link_nem_o_token(chatwoot):
    respx.get(AUDIO).mock(side_effect=httpx.ConnectError(f"caiu em {AUDIO}"))

    falha = _baixar(chatwoot)

    assert "abc" not in repr(falha) and "segredo" not in repr(falha)


def test_a_falsa_devolve_bytes_fixos_e_guarda_o_link():
    falso = ChatwootFalso()

    baixado = falso.baixar_anexo(AUDIO, aceitos=ACEITOS, max_bytes=LIMITE, prazo_s=15.0)

    assert isinstance(baixado, AnexoBaixado) and baixado.dados and baixado.content_type
    assert falso.baixados == [AUDIO]
    falso.anexo = FalhaDownload("status")
    outra = falso.baixar_anexo(AUDIO, aceitos=ACEITOS, max_bytes=LIMITE, prazo_s=1.0)
    assert outra == FalhaDownload("status")
