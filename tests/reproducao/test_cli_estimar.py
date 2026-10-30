"""O comando `agente estimar-carteira` (tarefa 10.6): da pasta salva e do Chatwoot simulado."""

import csv
import json
from datetime import UTC, datetime

import pytest

import agente.cli
from agente.cli import main
from agente.reproducao.chatwoot_leitura import ChatwootLeitura

from ..conftest import RAIZ
from .fabrica import CONTA, INBOX, SEGUNDA_10H, TOKEN, ChatwootSimulado, conversa, mensagem

URL = "https://chatwoot.invalid"
AGORA = datetime.fromtimestamp(SEGUNDA_10H + 86400, UTC)
DEBITO = "Olá, seu débito é de R$ 100,20. Com esse mega desconto pagando agora fica por R$ 85,00"


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(RAIZ / "politica.toml"))
    monkeypatch.setenv("AGENTE_CHATWOOT_URL", URL)
    monkeypatch.setenv("AGENTE_CHATWOOT_CONTA", str(CONTA))
    monkeypatch.setenv("AGENTE_CHATWOOT_INBOX", str(INBOX))
    monkeypatch.setenv("AGENTE_CHATWOOT_TOKEN", TOKEN)
    monkeypatch.setattr(agente.cli, "_agora", lambda: AGORA)
    return tmp_path


def _pasta_salva(pasta, ids_e_telefones: dict[int, str]):
    (pasta / "inbox198").mkdir(parents=True)
    for id_, telefone in ids_e_telefones.items():
        corpo = {
            "conversa": conversa(id_, SEGUNDA_10H, telefone=telefone),
            "mensagens": [mensagem(id_ * 10, id_, 1, DEBITO, SEGUNDA_10H)],
        }
        (pasta / "inbox198" / f"conv{id_}.json").write_text(json.dumps(corpo), encoding="utf-8")


def _com_servidor(monkeypatch, servidor: ChatwootSimulado, token: str = TOKEN) -> None:
    monkeypatch.setattr(
        agente.cli,
        "_abrir_leitura",
        lambda config: ChatwootLeitura(
            config.chatwoot_url,
            config.chatwoot_conta,
            token,
            transporte=servidor.transporte,
        ),
    )


def test_da_pasta_grava_o_csv_e_imprime_so_o_agregado(ambiente, capsys):
    _pasta_salva(ambiente / "varredura", {1: "+5583999990001", 2: "+5583999990002"})
    saida = ambiente / "pre-sombra" / "carteira-estimada.csv"

    codigo = main(
        ["estimar-carteira", "--da-pasta", str(ambiente / "varredura"), "--saida", str(saida)]
    )

    impresso = capsys.readouterr()
    linhas = list(csv.DictReader(saida.open(encoding="utf-8"), delimiter=";"))
    assert codigo == 0
    assert [linha["mensalidades_abertas"] for linha in linhas] == ["3", "3"]
    assert "conversas lidas: 2" in impresso.out
    assert "telefones únicos: 2" in impresso.out
    assert "3: 2" in impresso.out
    tudo = impresso.out + impresso.err
    assert "99999000" not in tudo
    assert "Fulano" not in tudo
    assert "mega desconto" not in tudo


def test_da_pasta_nao_toca_na_rede(ambiente, monkeypatch):
    _pasta_salva(ambiente / "varredura", {1: "+5583999990001"})

    def proibido(config):
        raise AssertionError("a pasta não pode abrir o Chatwoot")

    monkeypatch.setattr(agente.cli, "_abrir_leitura", proibido)

    codigo = main(["estimar-carteira", "--da-pasta", "varredura", "--saida", "c.csv"])

    assert codigo == 0


def test_do_chatwoot_so_faz_get_e_respeita_os_dias(ambiente, monkeypatch, capsys):
    servidor = ChatwootSimulado(
        [
            conversa(1, SEGUNDA_10H),
            conversa(2, SEGUNDA_10H - 10 * 86400, telefone="+5583999990002"),
        ],
        {
            1: [mensagem(1, 1, 1, DEBITO, SEGUNDA_10H)],
            2: [mensagem(2, 2, 1, DEBITO, SEGUNDA_10H - 10 * 86400)],
        },
    )
    _com_servidor(monkeypatch, servidor)

    codigo = main(["estimar-carteira", "--dias", "3", "--saida", "c.csv"])

    assert codigo == 0
    assert {pedido.method for pedido in servidor.pedidos} == {"GET"}
    assert "conversas lidas: 1" in capsys.readouterr().out


def test_do_chatwoot_pede_as_variaveis_que_faltam(ambiente, monkeypatch, capsys):
    servidor = ChatwootSimulado([], {})
    _com_servidor(monkeypatch, servidor)
    monkeypatch.delenv("AGENTE_CHATWOOT_TOKEN")

    codigo = main(["estimar-carteira", "--saida", "c.csv"])

    assert codigo == 2
    assert "AGENTE_CHATWOOT_TOKEN" in capsys.readouterr().out
    assert servidor.pedidos == []


def test_recusa_o_csv_que_ja_existe_sem_ler_nada(ambiente, monkeypatch, capsys):
    servidor = ChatwootSimulado([], {})
    _com_servidor(monkeypatch, servidor)
    (ambiente / "c.csv").write_text("antigo", encoding="utf-8")

    codigo = main(["estimar-carteira", "--saida", "c.csv"])

    assert codigo == 2
    assert "c.csv" in capsys.readouterr().out
    assert (ambiente / "c.csv").read_text(encoding="utf-8") == "antigo"
    assert servidor.pedidos == []


def test_recusa_a_pasta_que_nao_existe(ambiente, capsys):
    codigo = main(["estimar-carteira", "--da-pasta", "nao-existe", "--saida", "c.csv"])

    assert codigo == 2
    assert "nao-existe" in capsys.readouterr().out
    assert not (ambiente / "c.csv").exists()


def test_arquivo_estragado_na_pasta_para_sem_gravar_o_csv(ambiente, capsys):
    (ambiente / "varredura").mkdir()
    (ambiente / "varredura" / "conv1.json").write_text("{}", encoding="utf-8")

    codigo = main(["estimar-carteira", "--da-pasta", "varredura", "--saida", "c.csv"])

    assert codigo == 2
    assert "conv1.json" in capsys.readouterr().out
    assert not (ambiente / "c.csv").exists()


def test_dias_e_da_pasta_nao_andam_juntos(ambiente):
    with pytest.raises(SystemExit):
        main(["estimar-carteira", "--da-pasta", "x", "--dias", "3", "--saida", "c.csv"])


def test_token_recusado_diz_o_codigo_e_nunca_o_token(ambiente, monkeypatch, capsys):
    _com_servidor(monkeypatch, ChatwootSimulado([], {}), token="token-errado")

    codigo = main(["estimar-carteira", "--saida", "c.csv"])

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "401" in saida
    assert "token-errado" not in saida
    assert not (ambiente / "c.csv").exists()
