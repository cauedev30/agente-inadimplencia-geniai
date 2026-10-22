import json

import httpx
import pytest
import respx

from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
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
