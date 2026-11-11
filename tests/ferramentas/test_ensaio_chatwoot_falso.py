"""O Chatwoot falso do ensaio: o `ChatwootHttp` de verdade conversa com ele, em 127.0.0.1."""

import importlib.util
import sys

import httpx
import pytest

from agente.integracoes.chatwoot import ChatwootHttp
from agente.tipos import Botao

from ..conftest import RAIZ

_CAMINHO = RAIZ / "ferramentas" / "demo-whatsapp" / "chatwoot_falso.py"
_spec = importlib.util.spec_from_file_location("demo_whatsapp_chatwoot_falso", _CAMINHO)
falso_modulo = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = falso_modulo
_spec.loader.exec_module(falso_modulo)

CONTA = 91
CONVERSA = 9001


@pytest.fixture
def falso():
    servidor = falso_modulo.ChatwootFalsoLocal(CONTA)
    servidor.iniciar()
    yield servidor
    servidor.parar()


@pytest.fixture
def cliente(falso):
    with ChatwootHttp(falso.url, CONTA, "token-ficticio") as http:
        yield http


def _tipos(falso):
    return [registro.tipo for registro in falso.registros]


def test_o_endereco_e_local_e_a_porta_vem_do_sistema(falso):
    assert falso.url.startswith("http://127.0.0.1:")
    assert int(falso.url.rsplit(":", 1)[1]) > 0


def test_enviar_mensagem_devolve_o_id_e_grava_a_mensagem(falso, cliente):
    primeiro = cliente.enviar_mensagem(CONVERSA, "Olá")
    segundo = cliente.enviar_mensagem(CONVERSA, "De novo")
    assert primeiro != segundo
    assert [(r.conversa_id, r.tipo, r.conteudo) for r in falso.registros] == [
        (CONVERSA, "mensagem", "Olá"),
        (CONVERSA, "mensagem", "De novo"),
    ]


def test_a_mensagem_com_botao_de_link_leva_o_link_no_texto(falso, cliente):
    cliente.enviar_mensagem(CONVERSA, "Pague", [Botao("url", "Pagar", "https://x.invalid/p")])
    assert "https://x.invalid/p" in falso.registros[0].conteudo


def test_a_nota_privada_e_uma_nota_e_nao_aparece_nas_mensagens_do_agente(falso, cliente):
    cliente.enviar_mensagem(CONVERSA, "Para o cliente")
    cliente.nota_privada(CONVERSA, "Para o atendente")
    assert _tipos(falso) == ["mensagem", "nota"]
    assert falso.registros[1].conteudo == "Para o atendente"
    assert [texto for _id, texto in cliente.mensagens_do_agente(CONVERSA)] == ["Para o cliente"]


def test_mensagens_do_agente_so_devolve_as_da_conversa_pedida(falso, cliente):
    cliente.enviar_mensagem(CONVERSA, "Daqui")
    cliente.enviar_mensagem(CONVERSA + 1, "De outra")
    assert [texto for _id, texto in cliente.mensagens_do_agente(CONVERSA)] == ["Daqui"]


def test_etiquetar_soma_as_etiquetas_e_registra_a_lista_inteira(falso, cliente):
    cliente.etiquetar(CONVERSA, ["desconfianca"])
    cliente.etiquetar(CONVERSA, ["teto", "desconfianca"])
    registros = [r for r in falso.registros if r.tipo == "etiquetas"]
    assert [r.conteudo for r in registros] == [["desconfianca"], ["desconfianca", "teto"]]
    assert falso.etiquetas(CONVERSA) == ["desconfianca", "teto"]
    assert falso.etiquetas(CONVERSA + 1) == []


def test_atribuir_mudar_status_e_atributos_gravam_cada_um_o_seu_registro(falso, cliente):
    cliente.atribuir(CONVERSA, 7)
    cliente.mudar_status(CONVERSA, "open")
    cliente.gravar_atributos(CONVERSA, {"matricula": "ENS-0001"})
    assert [(r.tipo, r.conteudo) for r in falso.registros] == [
        ("atribuicao", 7),
        ("status", "open"),
        ("atributos", {"matricula": "ENS-0001"}),
    ]


def test_registros_desde_devolve_so_o_que_veio_depois_do_momento(falso, cliente):
    cliente.enviar_mensagem(CONVERSA, "antes")
    corte = falso.registros[-1].momento
    cliente.enviar_mensagem(CONVERSA, "depois")
    assert [r.conteudo for r in falso.registros_desde(corte)] == ["depois"]


def test_conta_errada_e_404_e_aparece_como_erro_no_registro(falso):
    resposta = httpx.post(
        f"{falso.url}/api/v1/accounts/{CONTA + 1}/conversations/{CONVERSA}/messages",
        json={"content": "x"},
    )
    assert resposta.status_code == 404
    assert _tipos(falso) == ["erro"]
    assert "accounts/92" in falso.registros[0].conteudo


def test_rota_desconhecida_e_404_e_aparece_como_erro_no_registro(falso):
    resposta = httpx.get(f"{falso.url}/api/v1/accounts/{CONTA}/conversations/{CONVERSA}/outra")
    assert resposta.status_code == 404
    assert _tipos(falso) == ["erro"]


def test_parar_derruba_o_servidor(falso):
    url = falso.url
    falso.parar()
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{url}/api/v1/accounts/{CONTA}/conversations/{CONVERSA}/labels", timeout=2)
