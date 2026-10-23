import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.politica import Regua
from agente.tipos import EstadoConversa, StatusCobranca
from agente.webchat.token import emitir_token

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
SEGREDO = "segredo-do-webchat-com-32-caracteres-ou-mais"
CHAVE_HMAC = "chave-hmac-da-inbox-super-secreta"
WEBSITE_TOKEN = "token-publico-da-inbox"
CPF = "12345678909"
NOME = "Maria Fictícia da Silva"
TELEFONE = "+5583999990001"


@pytest.fixture
def fabrica():
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-1", nome=NOME, telefone=TELEFONE)
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="outubro",
        )
        sessao.add(
            Conversa(
                cobranca=cobranca,
                chatwoot_conversa_id=555,
                estado=EstadoConversa.ATENDIMENTO_HUMANO,
            )
        )
        sessao.commit()
    return fabrica


@pytest.fixture
def ctn():
    return CTNFalso([ClienteCTN("F-1", NOME, TELEFONE, cpf=CPF)])


class Relogio:
    def __init__(self, agora=AGORA):
        self.agora = agora

    def __call__(self):
        return self.agora


class ChatwootFora(ChatwootFalso):
    def nota_privada(self, conversa_id, texto):
        raise httpx.ConnectError("fora do ar")


def _app(fabrica, politica, ctn, relogio=None, chatwoot=None, **config):
    campos = {
        "database_url": "sqlite://",
        "chatwoot_url": "https://chat.invalid",
        "webchat_segredo": SEGREDO,
        "webchat_identidade_hmac": CHAVE_HMAC,
        "webchat_website_token": WEBSITE_TOKEN,
    }
    return criar_app(
        fabrica,
        politica,
        Config(**{**campos, **config}),
        relogio=relogio or (lambda: AGORA),
        iniciar_trabalhador=False,
        ctn=ctn,
        dependencias=SimpleNamespace(chatwoot=chatwoot) if chatwoot else None,
    )


@pytest.fixture
def http(fabrica, politica, ctn):
    with TestClient(_app(fabrica, politica, ctn)) as cliente:
        yield cliente


@pytest.fixture
def token():
    return emitir_token(SEGREDO, 1, AGORA + timedelta(days=30))


def _confirmar(http, token, digitos):
    return http.post(
        f"/atendimento/{token}",
        content=f"cpf={digitos}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def _sem_cache(resposta):
    assert resposta.headers["cache-control"] == "no-store"


def test_o_token_valido_pede_os_digitos_do_cpf(http, token):
    resposta = http.get(f"/atendimento/{token}")

    assert resposta.status_code == 200
    _sem_cache(resposta)
    assert "text/html" in resposta.headers["content-type"]
    assert 'name="cpf"' in resposta.text
    assert "3 primeiros" in resposta.text
    assert "sdk.js" not in resposta.text  # o widget só carrega depois do CPF


@pytest.mark.parametrize("token_ruim", ["lixo", "A" * 60])
def test_o_token_invalido_responde_404(http, token_ruim):
    resposta = http.get(f"/atendimento/{token_ruim}")

    assert resposta.status_code == 404
    _sem_cache(resposta)
    assert _confirmar(http, token_ruim, CPF[:3]).status_code == 404


def test_o_token_vencido_responde_404(fabrica, politica, ctn):
    vencido = emitir_token(SEGREDO, 1, AGORA - timedelta(seconds=1))
    with TestClient(_app(fabrica, politica, ctn)) as http:
        assert http.get(f"/atendimento/{vencido}").status_code == 404


def test_o_token_revogado_responde_404_igual_ao_invalido(http, fabrica, token):
    invalido = http.get("/atendimento/lixo")
    with fabrica() as sessao:
        sessao.get(Cobranca, 1).status = StatusCobranca.PAGA
        sessao.commit()

    revogado = http.get(f"/atendimento/{token}")

    assert revogado.status_code == 404
    assert revogado.text == invalido.text


def test_os_digitos_certos_carregam_o_widget_com_a_identidade(http, token):
    resposta = _confirmar(http, token, CPF[:3])

    assert resposta.status_code == 200
    _sem_cache(resposta)
    esperado = hmac.new(CHAVE_HMAC.encode(), b"cobranca-1", hashlib.sha256).hexdigest()
    assert WEBSITE_TOKEN in resposta.text
    assert "https://chat.invalid" in resposta.text
    assert "/packs/js/sdk.js" in resposta.text
    assert "cobranca-1" in resposta.text
    assert esperado in resposta.text
    assert "setUser" in resposta.text


def test_a_chave_do_hmac_e_o_segredo_nunca_aparecem(http, token):
    for resposta in (http.get(f"/atendimento/{token}"), _confirmar(http, token, CPF[:3])):
        assert CHAVE_HMAC not in resposta.text
        assert SEGREDO not in resposta.text
        assert CHAVE_HMAC not in str(resposta.headers)


def test_nao_ha_dado_sensivel_no_html_nem_na_url(http, token):
    for resposta in (
        http.get(f"/atendimento/{token}"),
        _confirmar(http, token, CPF[:3]),
        _confirmar(http, token, "000"),
    ):
        corpo = resposta.text
        assert "100,20" not in corpo and "10020" not in corpo and "R$" not in corpo
        assert NOME not in corpo and "Maria" not in corpo
        assert TELEFONE not in corpo and "999990001" not in corpo
        assert CPF not in corpo
        assert "<title>Atendimento</title>" in corpo
        assert resposta.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in resposta.headers["x-robots-tag"]
    assert "10020" not in token and "Maria" not in token


def test_digitos_errados_nao_carregam_o_widget(http, token):
    resposta = _confirmar(http, token, "000")

    assert resposta.status_code == 200
    assert "sdk.js" not in resposta.text
    assert "não conferem" in resposta.text
    assert 'name="cpf"' in resposta.text


def test_tres_erros_bloqueiam_ate_o_digito_certo(http, token):
    for _ in range(2):
        assert _confirmar(http, token, "000").status_code == 200
    assert _confirmar(http, token, "000").status_code == 403  # o 3º erro já mostra o bloqueio

    for digitos in ("000", CPF[:3]):
        resposta = _confirmar(http, token, digitos)
        assert resposta.status_code == 403
        assert "sdk.js" not in resposta.text
        _sem_cache(resposta)
    assert http.get(f"/atendimento/{token}").status_code == 403


def test_o_acerto_do_cpf_grava_a_verificacao(http, fabrica, token):
    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_verificado_em is None

    _confirmar(http, token, "000")
    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_verificado_em is None
    _confirmar(http, token, CPF[:3])

    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_verificado_em == AGORA


def test_o_acerto_zera_as_tentativas(http, token):
    for _ in range(2):
        _confirmar(http, token, "000")
    assert "sdk.js" in _confirmar(http, token, CPF[:3]).text

    for _ in range(2):
        assert _confirmar(http, token, "000").status_code == 200
    assert "sdk.js" in _confirmar(http, token, CPF[:3]).text


@pytest.mark.parametrize("digitos", ["", "12", "1234", "abc", "１２３", "000"])
def test_o_formato_errado_conta_como_erro(http, token, digitos):
    resposta = _confirmar(http, token, digitos)

    assert resposta.status_code == 200
    assert "sdk.js" not in resposta.text


def test_o_bloqueio_vale_por_cobranca_e_nao_por_token(http, token):
    for _ in range(3):
        _confirmar(http, token, "000")
    outro = emitir_token(SEGREDO, 1, AGORA + timedelta(days=5))

    assert _confirmar(http, outro, CPF[:3]).status_code == 403


def test_cliente_sem_cpf_no_ctn_nao_abre_nem_gasta_as_tentativas(fabrica, politica):
    ctn = CTNFalso([ClienteCTN("F-1", NOME, TELEFONE, cpf=None)])
    token = emitir_token(SEGREDO, 1, AGORA + timedelta(days=1))
    with TestClient(_app(fabrica, politica, ctn)) as http:
        for _ in range(5):
            resposta = _confirmar(http, token, "000")

            assert resposta.status_code == 503
            assert "sdk.js" not in resposta.text
            assert "WhatsApp" in resposta.text
            assert "CPF" not in resposta.text
    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_tentativas_cpf == 0


def test_ctn_fora_do_ar_responde_503_sem_gastar_tentativa(http, ctn, token):
    ctn.fora_do_ar = True
    assert _confirmar(http, token, CPF[:3]).status_code == 503

    ctn.fora_do_ar = False
    for _ in range(2):
        _confirmar(http, token, "000")
    assert "sdk.js" in _confirmar(http, token, CPF[:3]).text


@pytest.mark.parametrize(
    "faltando", ["webchat_segredo", "webchat_identidade_hmac", "webchat_website_token"]
)
def test_sem_segredo_ou_vazio_a_pagina_fica_desligada(fabrica, politica, ctn, faltando):
    token = emitir_token(SEGREDO, 1, AGORA + timedelta(days=1))
    for valor in (None, "", "  "):
        with TestClient(_app(fabrica, politica, ctn, **{faltando: valor})) as http:
            assert http.get(f"/atendimento/{token}").status_code == 503
            assert _confirmar(http, token, CPF[:3]).status_code == 503


def test_sem_o_chatwoot_ou_o_ctn_a_pagina_fica_desligada(fabrica, politica, ctn, token):
    with TestClient(_app(fabrica, politica, ctn, chatwoot_url=None)) as http:
        assert http.get(f"/atendimento/{token}").status_code == 503
    with TestClient(_app(fabrica, politica, None)) as http:
        assert http.get(f"/atendimento/{token}").status_code == 503


def test_os_valores_no_script_nao_quebram_o_html(fabrica, politica, ctn, token):
    config = {"webchat_website_token": "</script><b>x"}
    with TestClient(_app(fabrica, politica, ctn, **config)) as http:
        corpo = _confirmar(http, token, CPF[:3]).text

    assert "</script><b>" not in corpo
    assert len(re.findall("</script>", corpo)) == len(re.findall("<script", corpo))


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def chatwoot():
    return ChatwootFalso()


@pytest.fixture
def http_com_relogio(fabrica, politica, ctn, relogio, chatwoot):
    with TestClient(_app(fabrica, politica, ctn, relogio=relogio, chatwoot=chatwoot)) as cliente:
        yield cliente


def _errar(http, token, vezes):
    for _ in range(vezes):
        _confirmar(http, token, "000")


def test_o_bloqueio_dura_uma_hora_e_depois_as_tentativas_zeram(
    http_com_relogio, relogio, politica, token
):
    http = http_com_relogio
    assert politica.webchat_cpf_bloqueio_horas == 1
    _errar(http, token, 3)

    relogio.agora = AGORA + timedelta(minutes=59)
    assert http.get(f"/atendimento/{token}").status_code == 403
    assert _confirmar(http, token, CPF[:3]).status_code == 403

    relogio.agora = AGORA + timedelta(hours=1, seconds=1)
    assert http.get(f"/atendimento/{token}").status_code == 200
    for _ in range(2):
        assert _confirmar(http, token, "000").status_code == 200
    assert _confirmar(http, token, "000").status_code == 403
    assert _confirmar(http, token, CPF[:3]).status_code == 403


def test_depois_do_bloqueio_o_digito_certo_abre(http_com_relogio, relogio, token):
    _errar(http_com_relogio, token, 3)
    relogio.agora = AGORA + timedelta(hours=2)

    assert "sdk.js" in _confirmar(http_com_relogio, token, CPF[:3]).text


def test_o_bloqueio_deixa_nota_e_etiqueta_na_conversa_uma_vez_so(
    http_com_relogio, chatwoot, relogio, token
):
    _errar(http_com_relogio, token, 2)
    assert chatwoot.chamadas == []

    _errar(http_com_relogio, token, 5)

    assert [(c[0], c[1]) for c in chatwoot.chamadas] == [
        ("etiquetar", 555),
        ("nota_privada", 555),
    ]
    assert chatwoot.chamadas[0][2] == ["webchat_bloqueado"]
    assert CPF not in str(chatwoot.chamadas)

    relogio.agora = AGORA + timedelta(hours=2)
    _errar(http_com_relogio, token, 3)
    assert sum(1 for c in chatwoot.chamadas if c[0] == "nota_privada") == 2


def test_o_chatwoot_fora_no_aviso_nao_derruba_a_pagina(fabrica, politica, ctn, token):
    with TestClient(_app(fabrica, politica, ctn, chatwoot=ChatwootFora())) as http:
        _errar(http, token, 2)
        resposta = _confirmar(http, token, "000")

        assert resposta.status_code == 403
        assert _confirmar(http, token, CPF[:3]).status_code == 403


def test_erros_acumulados_acima_de_um_teto_menor_ganham_prazo_e_nao_prendem(
    http_com_relogio, fabrica, relogio, token
):
    with fabrica() as sessao:
        sessao.get(Cobranca, 1).webchat_tentativas_cpf = 3  # o teto já foi 5, por exemplo
        sessao.commit()

    assert http_com_relogio.get(f"/atendimento/{token}").status_code == 403
    with fabrica() as sessao:
        assert sessao.get(Cobranca, 1).webchat_bloqueado_ate == AGORA + timedelta(hours=1)

    relogio.agora = AGORA + timedelta(hours=1, seconds=1)
    assert http_com_relogio.get(f"/atendimento/{token}").status_code == 200
    assert "sdk.js" in _confirmar(http_com_relogio, token, CPF[:3]).text
