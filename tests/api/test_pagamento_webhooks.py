import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agente.api import ctn_webhook, pix_webhook
from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import Base
from agente.db.sessao import criar_sessao
from agente.pagamento.confirmar import ResultadoConfirmacao

SEGREDOS = {"pix": "segredo-pix", "ctn": "segredo-ctn"}
PAYLOAD = {
    "referencia": "agi0000000001abcdef",
    "valor_centavos": 10020,
    "pago_em": "2026-10-26T13:05:00+00:00",
}


def _assinar(corpo: bytes, segredo: str) -> str:
    return hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


@pytest.fixture
def chamadas(monkeypatch):
    """Substitui o `confirmar_pagamento` (o corpo é da 6.1) e guarda o que os webhooks mandaram."""
    registro = {"chamadas": [], "resultado": ResultadoConfirmacao.CONFIRMADO}

    def falso(sessao, deps, referencia, valor_centavos, pago_em):
        registro["chamadas"].append((deps, referencia, valor_centavos, pago_em))
        return registro["resultado"]

    monkeypatch.setattr(pix_webhook, "confirmar_pagamento", falso)
    monkeypatch.setattr(ctn_webhook, "confirmar_pagamento", falso)
    return registro


def _app(politica, **config):
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    campos = {
        "database_url": "sqlite://",
        "pix_webhook_segredo": SEGREDOS["pix"],
        "ctn_webhook_segredo": SEGREDOS["ctn"],
    }
    return criar_app(
        fabrica,
        politica,
        Config(**{**campos, **config}),
        dependencias="deps",
        iniciar_trabalhador=False,
    )


@pytest.fixture
def cliente_http(politica, chamadas):
    with TestClient(_app(politica)) as cliente:
        yield cliente


def _post(cliente, origem, corpo=None, assinatura="certa", cabecalho="X-Assinatura"):
    bruto = json.dumps(PAYLOAD if corpo is None else corpo).encode()
    if assinatura == "certa":
        assinatura = _assinar(bruto, SEGREDOS[origem])
    headers = {"Content-Type": "application/json"}
    if assinatura is not None:
        headers[cabecalho] = assinatura
    return cliente.post(f"/webhooks/{origem}", content=bruto, headers=headers)


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_payload_assinado_confirma_o_pagamento(cliente_http, chamadas, origem):
    resposta = _post(cliente_http, origem)
    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "CONFIRMADO"}
    deps, referencia, valor, pago_em = chamadas["chamadas"][0]
    assert (deps, referencia, valor) == ("deps", PAYLOAD["referencia"], 10020)
    assert pago_em == datetime(2026, 10, 26, 13, 5, tzinfo=UTC)


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_assinatura_com_prefixo_sha256_tambem_vale(cliente_http, chamadas, origem):
    bruto = json.dumps(PAYLOAD).encode()
    resposta = _post(cliente_http, origem, assinatura="sha256=" + _assinar(bruto, SEGREDOS[origem]))
    assert resposta.status_code == 200


@pytest.mark.parametrize("origem", ["pix", "ctn"])
@pytest.mark.parametrize("assinatura", [None, "errada", ""])
def test_sem_assinatura_ou_com_a_errada_responde_401(cliente_http, chamadas, origem, assinatura):
    resposta = _post(cliente_http, origem, assinatura=assinatura)
    assert resposta.status_code == 401
    assert chamadas["chamadas"] == []


def test_assinatura_do_outro_webhook_nao_vale(cliente_http, chamadas):
    bruto = json.dumps(PAYLOAD).encode()
    resposta = _post(cliente_http, "pix", assinatura=_assinar(bruto, SEGREDOS["ctn"]))
    assert resposta.status_code == 401


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_corpo_alterado_depois_de_assinado_responde_401(cliente_http, chamadas, origem):
    assinatura = _assinar(json.dumps(PAYLOAD).encode(), SEGREDOS[origem])
    adulterado = {**PAYLOAD, "valor_centavos": 1}
    assert _post(cliente_http, origem, adulterado, assinatura).status_code == 401


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_sem_segredo_configurado_o_webhook_fica_desligado(politica, chamadas, origem):
    campo = f"{origem}_webhook_segredo"
    with TestClient(_app(politica, **{campo: None})) as cliente:
        resposta = _post(cliente, origem, assinatura="qualquer")
    assert resposta.status_code == 503
    assert chamadas["chamadas"] == []


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_segredo_vazio_no_ambiente_desliga_o_webhook(politica, chamadas, monkeypatch, origem):
    # O .env copiado do .env.example traz a variável vazia: não pode ligar o HMAC com chave vazia.
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite://")
    monkeypatch.setenv(f"AGENTE_{origem.upper()}_WEBHOOK_SEGREDO", "")
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    app = criar_app(
        fabrica, politica, Config(_env_file=None), dependencias="deps", iniciar_trabalhador=False
    )
    corpo = json.dumps(PAYLOAD).encode()
    with TestClient(app) as cliente:
        resposta = cliente.post(
            f"/webhooks/{origem}", content=corpo, headers={"X-Assinatura": _assinar(corpo, "")}
        )
    assert resposta.status_code == 503
    assert chamadas["chamadas"] == []


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_segredo_vazio_na_rota_tambem_desliga_o_webhook(politica, chamadas, origem):
    with TestClient(_app(politica, **{f"{origem}_webhook_segredo": ""})) as cliente:
        corpo = json.dumps(PAYLOAD).encode()
        resposta = cliente.post(
            f"/webhooks/{origem}", content=corpo, headers={"X-Assinatura": _assinar(corpo, "")}
        )
    assert resposta.status_code == 503


def test_so_o_webhook_sem_segredo_fica_desligado(politica, chamadas):
    with TestClient(_app(politica, ctn_webhook_segredo=None)) as cliente:
        assert _post(cliente, "pix").status_code == 200
        assert _post(cliente, "ctn").status_code == 503


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_repetido_responde_200(cliente_http, chamadas, origem):
    chamadas["resultado"] = ResultadoConfirmacao.JA_CONFIRMADO
    resposta = _post(cliente_http, origem)
    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "JA_CONFIRMADO"}


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_valor_divergente_responde_200_e_fica_registrado(cliente_http, chamadas, caplog, origem):
    chamadas["resultado"] = ResultadoConfirmacao.VALOR_DIVERGENTE
    with caplog.at_level("WARNING"):
        resposta = _post(cliente_http, origem)
    assert resposta.status_code == 200
    assert resposta.json() == {"resultado": "VALOR_DIVERGENTE"}
    assert PAYLOAD["referencia"] in caplog.text
    assert "10020" in caplog.text


@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_referencia_desconhecida_responde_404(cliente_http, chamadas, origem):
    chamadas["resultado"] = ResultadoConfirmacao.DESCONHECIDO
    assert _post(cliente_http, origem).status_code == 404


@pytest.mark.parametrize(
    "corpo",
    [
        {"valor_centavos": 10020, "pago_em": PAYLOAD["pago_em"]},
        {**PAYLOAD, "valor_centavos": "muito"},
        {**PAYLOAD, "valor_centavos": 0},
        {**PAYLOAD, "pago_em": "ontem"},
        {**PAYLOAD, "pago_em": "2026-10-26T13:05:00"},  # sem fuso
        {**PAYLOAD, "referencia": ""},
        [1, 2],
    ],
)
@pytest.mark.parametrize("origem", ["pix", "ctn"])
def test_corpo_invalido_responde_422(cliente_http, chamadas, origem, corpo):
    assert _post(cliente_http, origem, corpo).status_code == 422
    assert chamadas["chamadas"] == []


def test_json_quebrado_assinado_responde_422(cliente_http, chamadas):
    bruto = b"{nao e json"
    resposta = cliente_http.post(
        "/webhooks/pix", content=bruto, headers={"X-Assinatura": _assinar(bruto, SEGREDOS["pix"])}
    )
    assert resposta.status_code == 422


def test_o_log_nao_leva_cpf_nem_telefone(cliente_http, chamadas, caplog):
    chamadas["resultado"] = ResultadoConfirmacao.VALOR_DIVERGENTE
    corpo = {**PAYLOAD, "cpf": "12345678901", "telefone": "+5583999990001"}
    with caplog.at_level("DEBUG"):
        _post(cliente_http, "pix", corpo)
    assert "12345678901" not in caplog.text
    assert "999990001" not in caplog.text


def test_o_servico_de_producao_liga_os_webhooks_com_as_dependencias(monkeypatch):
    from agente.api.principal import montar_app

    from ..conftest import RAIZ

    config = Config(
        database_url="sqlite://",
        politica_path=RAIZ / "politica.toml",
        pix_webhook_segredo=SEGREDOS["pix"],
    )
    chamadas = []

    def falso(sessao, deps, referencia, valor_centavos, pago_em):
        chamadas.append(deps)
        return ResultadoConfirmacao.CONFIRMADO

    monkeypatch.setattr(pix_webhook, "confirmar_pagamento", falso)
    app = montar_app(config, iniciar_trabalhador=False)
    with TestClient(app) as cliente:
        assert _post(cliente, "pix").status_code == 200
        assert _post(cliente, "ctn").status_code == 503
    assert chamadas and chamadas[0] is not None
