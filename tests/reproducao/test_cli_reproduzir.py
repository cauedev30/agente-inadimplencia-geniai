"""O comando `agente reproduzir` (tarefa 10.5): contra o MockTransport, sem rede."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

import agente.cli
import agente.reproducao.reproduzir
from agente.cli import main
from agente.integracoes.jev_typesafe import JevTypeSafe
from agente.reproducao.chatwoot_leitura import ChatwootLeitura
from agente.reproducao.folha import AVISO_CARTEIRA_ESTIMADA

from ..conftest import RAIZ
from ..integracoes.test_jev_typesafe import handler_ok
from .fabrica import CONTA, INBOX, SEGUNDA_10H, TOKEN, ChatwootSimulado

FIXTURES = Path(__file__).parent / "fixtures"
CARTEIRA = FIXTURES / "carteira.csv"
URL = "https://chatwoot.invalid"
# Terça de manhã: as conversas do exemplo (segunda) estão dentro dos 3 dias.
AGORA = datetime.fromtimestamp(SEGUNDA_10H + 86400, UTC)


def _servidor() -> ChatwootSimulado:
    dados = json.loads((FIXTURES / "conversa_exemplo.json").read_text(encoding="utf-8"))
    bruta = dados["conversa"]
    return ChatwootSimulado([bruta], {bruta["id"]: dados["mensagens"]}, pagina=4)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """O diretório de trabalho é o tmp: nenhum .env do repositório entra na configuração."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(RAIZ / "politica.toml"))
    monkeypatch.setenv("AGENTE_CHATWOOT_URL", URL)
    monkeypatch.setenv("AGENTE_CHATWOOT_CONTA", str(CONTA))
    monkeypatch.setenv("AGENTE_CHATWOOT_INBOX", str(INBOX))
    monkeypatch.setenv("AGENTE_CHATWOOT_TOKEN", TOKEN)
    # O serviço de verdade usaria este banco; o comando não pode tocar nele.
    monkeypatch.setenv("AGENTE_DATABASE_URL", "sqlite:///nao-deve-ser-criado.db")
    monkeypatch.setattr(agente.cli, "_agora", lambda: AGORA)
    return tmp_path


def _com_servidor(monkeypatch, servidor: ChatwootSimulado) -> None:
    monkeypatch.setattr(
        agente.cli,
        "_abrir_leitura",
        lambda config: ChatwootLeitura(
            config.chatwoot_url,
            config.chatwoot_conta,
            config.chatwoot_token,
            transporte=servidor.transporte,
        ),
    )


def _comando(ambiente, *extra: str) -> list[str]:
    return [
        "reproduzir",
        "--carteira",
        str(CARTEIRA),
        "--dias",
        "3",
        "--saida",
        str(ambiente / "pre-sombra" / "saida"),
        *extra,
    ]


def test_roda_contra_o_mock_e_imprime_o_relatorio_e_o_caminho_da_folha(
    ambiente, monkeypatch, capsys
):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr().out
    pasta = ambiente / "pre-sombra" / "saida"
    assert codigo == 0
    assert (pasta / "reproducao.db").is_file()
    assert (pasta / "folha.md").is_file()
    assert str(pasta / "folha.md") in saida
    assert "conversas: 1 lidas, 1 cruzadas" in saida
    assert "turnos por resultado: SOMBRA: 2" in saida
    assert "INFORM_DEBT: 2" in saida
    # Só GET, com o token e a conta das variáveis; nada foi gravado no banco do serviço.
    assert {pedido.method for pedido in servidor.pedidos} == {"GET"}
    assert not (ambiente / "nao-deve-ser-criado.db").exists()
    # O corte pelos dias: a leitura pediu a inbox das variáveis.
    assert all(
        p.url.params["inbox_id"] == str(INBOX) for p in servidor.pedidos if "page" in p.url.params
    )


def test_a_saida_do_terminal_nao_leva_texto_nome_nem_telefone(ambiente, monkeypatch, capsys):
    _com_servidor(monkeypatch, _servidor())

    main(_comando(ambiente))

    saida = capsys.readouterr()
    tudo = saida.out + saida.err
    assert "quanto eu devo" not in tudo
    assert "Fulano" not in tudo
    assert "99999000" not in tudo
    assert TOKEN not in tudo


def test_a_folha_do_comando_tem_o_nome_mascarado(ambiente, monkeypatch):
    dados = json.loads((FIXTURES / "conversa_exemplo.json").read_text(encoding="utf-8"))
    dados["mensagens"][2]["content"] = "oi, sou o Fulano"
    servidor = ChatwootSimulado([dados["conversa"]], {901: dados["mensagens"]}, pagina=4)
    _com_servidor(monkeypatch, servidor)

    assert main(_comando(ambiente)) == 0

    folha = (ambiente / "pre-sombra" / "saida" / "folha.md").read_text(encoding="utf-8")
    assert "oi, sou o [nome]" in folha
    assert "Fulano" not in folha


def test_amostra_menor_limita_os_turnos_da_folha(ambiente, monkeypatch, capsys):
    _com_servidor(monkeypatch, _servidor())

    assert main(_comando(ambiente, "--amostra", "1")) == 0

    folha = (ambiente / "pre-sombra" / "saida" / "folha.md").read_text(encoding="utf-8")
    assert folha.count("## Turno ") == 1
    assert "(1 turnos;" in capsys.readouterr().out


@pytest.mark.parametrize(
    "variavel",
    [
        "AGENTE_CHATWOOT_URL",
        "AGENTE_CHATWOOT_CONTA",
        "AGENTE_CHATWOOT_INBOX",
        "AGENTE_CHATWOOT_TOKEN",
    ],
)
def test_recusa_clara_quando_falta_uma_variavel_do_chatwoot(
    ambiente, monkeypatch, capsys, variavel
):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)
    monkeypatch.delenv(variavel)

    codigo = main(_comando(ambiente))

    assert codigo == 2
    assert variavel in capsys.readouterr().out
    assert servidor.pedidos == []
    assert not (ambiente / "pre-sombra").exists()


def test_recusa_quando_o_banco_ja_existe_sem_ler_o_chatwoot(ambiente, monkeypatch, capsys):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)
    pasta = ambiente / "pre-sombra" / "saida"
    pasta.mkdir(parents=True)
    (pasta / "reproducao.db").write_bytes(b"antigo")

    codigo = main(_comando(ambiente))

    assert codigo == 2
    assert "reproducao.db" in capsys.readouterr().out
    assert servidor.pedidos == []
    assert (pasta / "reproducao.db").read_bytes() == b"antigo"


def test_recusa_quando_a_folha_ja_existe_e_nao_a_sobrescreve(ambiente, monkeypatch, capsys):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)
    pasta = ambiente / "pre-sombra" / "saida"
    pasta.mkdir(parents=True)
    (pasta / "folha.md").write_text("vereditos", encoding="utf-8")

    codigo = main(_comando(ambiente))

    assert codigo == 2
    assert "folha.md" in capsys.readouterr().out
    assert servidor.pedidos == []
    assert (pasta / "folha.md").read_text(encoding="utf-8") == "vereditos"


def test_recusa_a_carteira_que_nao_existe(ambiente, monkeypatch, capsys):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)

    codigo = main(
        [
            "reproduzir",
            "--carteira",
            str(ambiente / "nao-existe.csv"),
            "--saida",
            str(ambiente / "s"),
        ]
    )

    assert codigo == 2
    assert "carteira" in capsys.readouterr().out
    assert servidor.pedidos == []


def test_token_recusado_pelo_chatwoot_diz_o_codigo_e_nunca_o_token(ambiente, monkeypatch, capsys):
    servidor = _servidor()
    monkeypatch.setattr(
        agente.cli,
        "_abrir_leitura",
        lambda config: ChatwootLeitura(
            config.chatwoot_url,
            config.chatwoot_conta,
            "token-errado",
            transporte=servidor.transporte,
        ),
    )

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "401" in saida
    assert "token-errado" not in saida
    assert TOKEN not in saida
    assert not (ambiente / "pre-sombra" / "saida" / "folha.md").exists()


def test_falha_de_rede_nao_mostra_o_endereco(ambiente, monkeypatch, capsys):
    def cair(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede em chatwoot.invalid", request=pedido)

    monkeypatch.setattr(
        agente.cli,
        "_abrir_leitura",
        lambda config: ChatwootLeitura(
            config.chatwoot_url,
            config.chatwoot_conta,
            config.chatwoot_token,
            transporte=httpx.MockTransport(cair),
        ),
    )

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "ConnectError" in saida
    assert "chatwoot.invalid" not in saida


def test_dias_precisa_ser_positivo(ambiente, monkeypatch, capsys):
    _com_servidor(monkeypatch, _servidor())

    codigo = main(
        ["reproduzir", "--carteira", str(CARTEIRA), "--dias", "0", "--saida", str(ambiente / "s")]
    )

    assert codigo == 2
    assert "--dias" in capsys.readouterr().out


def test_carteira_estimada_poe_o_aviso_no_alto_da_folha(ambiente, monkeypatch):
    _com_servidor(monkeypatch, _servidor())

    assert main(_comando(ambiente, "--carteira-estimada")) == 0

    folha = (ambiente / "pre-sombra" / "saida" / "folha.md").read_text(encoding="utf-8")
    assert AVISO_CARTEIRA_ESTIMADA in folha


def test_sem_a_opcao_a_folha_nao_leva_o_aviso(ambiente, monkeypatch):
    _com_servidor(monkeypatch, _servidor())

    assert main(_comando(ambiente)) == 0

    folha = (ambiente / "pre-sombra" / "saida" / "folha.md").read_text(encoding="utf-8")
    assert AVISO_CARTEIRA_ESTIMADA not in folha


def test_com_a_chave_do_jev_e_sem_a_liberacao_sai_com_2_antes_de_ler_o_chatwoot(
    ambiente, monkeypatch, capsys
):
    servidor = _servidor()
    _com_servidor(monkeypatch, servidor)
    monkeypatch.setenv("AGENTE_JEV_API_KEY", "chave-ficticia")

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr().out
    assert codigo == 2
    assert "AGENTE_JEV_DADOS_REAIS_LIBERADOS" in saida
    assert "chave-ficticia" not in saida
    assert servidor.pedidos == []
    assert not (ambiente / "pre-sombra" / "saida").exists()


def test_com_a_chave_e_a_liberacao_a_reproducao_usa_o_jev_real(ambiente, monkeypatch, capsys):
    chamadas = []

    def handler(pedido):
        chamadas.append(pedido)
        return handler_ok(pedido)

    _com_servidor(monkeypatch, _servidor())
    monkeypatch.setenv("AGENTE_JEV_API_KEY", "chave-ficticia")
    monkeypatch.setenv("AGENTE_JEV_DADOS_REAIS_LIBERADOS", "true")
    monkeypatch.setattr(
        agente.cli,
        "_abrir_jev",
        lambda config, politica: JevTypeSafe.da_config(
            config, politica.jev_timeout_ms, transporte=httpx.MockTransport(handler)
        ),
    )

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr().out
    assert codigo == 0
    assert chamadas
    assert {c.url.host for c in chamadas} == {"api.typesafe.ai"}
    assert "Jev falso" not in saida and "typesafe:jev-1.14.0" in saida


SENSIVEL = "texto inventado do cliente que não pode ir ao terminal"


def test_erro_de_banco_na_reproducao_nao_imprime_os_parametros_do_sql(
    ambiente, monkeypatch, capsys
):
    # Um erro real do SQLAlchemy: sem `hide_parameters`, a mensagem traz `[parameters: (...)]`.
    def falha(sessao, *_resto):
        sessao.execute(
            text("INSERT INTO tabela_que_nao_existe (texto) VALUES (:t)"), {"t": SENSIVEL}
        )

    monkeypatch.setattr(agente.reproducao.reproduzir, "receber_evento", falha)
    _com_servidor(monkeypatch, _servidor())

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr()
    tudo = saida.out + saida.err
    assert codigo == 1
    assert SENSIVEL not in tudo
    assert "OperationalError" in tudo
    assert "apague a pasta de saída" in tudo.lower()
    assert "Traceback" not in tudo


def test_excecao_inesperada_diz_so_o_tipo_e_a_orientacao(ambiente, monkeypatch, capsys):
    def falha(*_args, **_kwargs):
        raise RuntimeError(SENSIVEL)

    monkeypatch.setattr(agente.cli, "reproduzir", falha)
    _com_servidor(monkeypatch, _servidor())

    codigo = main(_comando(ambiente))

    saida = capsys.readouterr()
    tudo = saida.out + saida.err
    assert codigo == 1
    assert SENSIVEL not in tudo
    assert "RuntimeError" in tudo
    assert "apague a pasta de saída" in tudo.lower()
    assert "Traceback" not in tudo


def _com_jev_real_que_conta_o_fechar(ambiente, monkeypatch) -> list:
    """Liberado e com a chave; o transporte é falso e o `fechar` do Jev é registrado."""
    fechados = []
    original = JevTypeSafe.fechar

    def fechar(self):
        fechados.append(self)
        original(self)

    monkeypatch.setenv("AGENTE_JEV_API_KEY", "chave-ficticia")
    monkeypatch.setenv("AGENTE_JEV_DADOS_REAIS_LIBERADOS", "true")
    monkeypatch.setattr(JevTypeSafe, "fechar", fechar)
    monkeypatch.setattr(
        agente.cli,
        "_abrir_jev",
        lambda config, politica: JevTypeSafe.da_config(
            config, politica.jev_timeout_ms, transporte=httpx.MockTransport(handler_ok)
        ),
    )
    return fechados


def test_a_reproducao_fecha_o_jev_real_ao_terminar(ambiente, monkeypatch):
    _com_servidor(monkeypatch, _servidor())
    fechados = _com_jev_real_que_conta_o_fechar(ambiente, monkeypatch)

    assert main(_comando(ambiente)) == 0

    assert len(fechados) == 1 and fechados[0]._cliente.is_closed


def test_a_reproducao_fecha_o_jev_real_mesmo_com_erro(ambiente, monkeypatch):
    def falha(*_args, **_kwargs):
        raise RuntimeError(SENSIVEL)

    monkeypatch.setattr(agente.cli, "reproduzir", falha)
    _com_servidor(monkeypatch, _servidor())
    fechados = _com_jev_real_que_conta_o_fechar(ambiente, monkeypatch)

    assert main(_comando(ambiente)) == 1

    assert len(fechados) == 1 and fechados[0]._cliente.is_closed
