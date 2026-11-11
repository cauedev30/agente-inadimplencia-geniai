"""`agente disparar` com o CTN real: só contagens e matrícula mascarada na saída (LGPD)."""

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command

from agente.cli import main
from agente.db.modelos import Cliente, Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso, MensalidadeAberta
from agente.integracoes.disparador import FalhaNoEnvio
from agente.politica import Regua

from .conftest import alembic_config, escrever_politica_de_teste, ligar_o_disparo

SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=ZoneInfo("America/Fortaleza"))
MATRICULA = "MAT-123456"
NOME = "Fulana Sigilosa"
TELEFONE = "+5583988887777"


@pytest.fixture
def banco(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)
    monkeypatch.setenv("AGENTE_CTN_API_KEY", "chave-ficticia")
    monkeypatch.setattr("agente.cli._agora", lambda: SEGUNDA_10H)
    ligar_o_disparo(tmp_path, monkeypatch, TELEFONE)
    with criar_sessao(url)() as sessao:
        cliente = Cliente(matricula_ctn=MATRICULA, nome=NOME, telefone=TELEFONE)
        sessao.add(
            Cobranca(
                cliente=cliente,
                regua=Regua.R3,
                mensalidades_abertas=3,
                divida_centavos=10020,
                propenso_cartao=False,
                campanha="nov",
            )
        )
        sessao.commit()
    return url


def _usar_ctn(monkeypatch, ctn):
    @contextmanager
    def abrir(config):
        yield ctn

    monkeypatch.setattr("agente.cli._abrir_ctn", abrir)


def _ctn_devendo():
    return CTNFalso(
        mensalidades_abertas={MATRICULA: [MensalidadeAberta("2026/10", 3340)]},
        hoje=SEGUNDA_10H.date(),
    )


def _sem_dado_de_cliente(saida):
    assert NOME not in saida and "Fulana" not in saida
    assert "+5583" not in saida and "988887777" not in saida
    assert MATRICULA not in saida


def test_disparar_imprime_os_contadores_novos(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn_devendo())

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "enviadas: 1" in saida
    for contador in (
        "puladas: 0",
        "fora da liberação (sem abertura): 0",
        "quitadas no CTN (sem abertura): 0",
        "sem lançamentos no CTN (sem abertura): 0",
        "dados suspeitos do CTN (sem abertura): 0",
        "não conferidas, CTN fora (sem abertura): 0",
        "telefone de mais de uma cobrança (sem abertura): 0",
        "falhas: 0",
    ):
        assert contador in saida
    assert "ALARME" not in saida
    _sem_dado_de_cliente(saida)


def test_disparar_com_ctn_fora_conta_as_nao_conferidas(banco, monkeypatch, capsys):
    ctn = _ctn_devendo()
    ctn.fora_do_ar = True
    _usar_ctn(monkeypatch, ctn)

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "enviadas: 0" in saida
    assert "não conferidas, CTN fora (sem abertura): 1" in saida


def test_disparar_avisa_do_alarme_da_conferencia(banco, monkeypatch, capsys):
    # 100% das conferências sem lançamento, mas só 1 (< 20): não há alarme.
    _usar_ctn(monkeypatch, CTNFalso(hoje=SEGUNDA_10H.date()))

    main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert "sem lançamentos no CTN (sem abertura): 1" in saida
    assert "ALARME" not in saida


def test_disparar_com_alarme_sai_com_codigo_diferente_e_avisa(banco, monkeypatch, capsys):
    monkeypatch.setattr("agente.abertura.disparar.ALARME_CONFERENCIA_MINIMO", 1)
    _usar_ctn(monkeypatch, CTNFalso(hoje=SEGUNDA_10H.date()))

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 3
    assert "ALARME DA CONFERÊNCIA" in saida
    _sem_dado_de_cliente(saida)


def test_falha_no_envio_sai_com_a_matricula_mascarada_e_sem_o_erro(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn_devendo())

    class DisparadorQueFalha:
        def enviar_template(self, telefone, template, variaveis, botoes):
            raise FalhaNoEnvio(f"HTTP 500 para {telefone} ({NOME}) {MATRICULA}")

    monkeypatch.setattr("agente.cli.DisparadorFalso", DisparadorQueFalha)

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "falhas: 1" in saida
    assert "***456" in saida
    assert "FalhaNoEnvio" in saida
    _sem_dado_de_cliente(saida)


def test_erro_inesperado_sai_so_com_o_tipo(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn_devendo())

    def estoura(*_, **__):
        raise RuntimeError(f"INSERT ... VALUES ('{NOME}', '{TELEFONE}', '{MATRICULA}')")

    monkeypatch.setattr("agente.cli.disparar_aberturas", estoura)

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "Erro inesperado (RuntimeError)" in saida
    _sem_dado_de_cliente(saida)


def test_a_sessao_do_disparo_oculta_os_parametros_do_sql(banco, monkeypatch):
    visto = {}
    original = criar_sessao

    def espiao(url, **opcoes):
        visto.update(opcoes)
        return original(url, **opcoes)

    monkeypatch.setattr("agente.cli.criar_sessao", espiao)
    _usar_ctn(monkeypatch, _ctn_devendo())

    main(["disparar", "--campanha", "nov"])

    assert visto.get("ocultar_parametros") is True


def test_disparar_conta_quem_esta_fora_da_liberacao(banco, tmp_path, monkeypatch, capsys):
    texto = (tmp_path / "politica-liberada.toml").read_text(encoding="utf-8")
    sem_ninguem = texto.replace(
        f'liberacao = {{ modo = "piloto", telefones = ["{TELEFONE}"] }}',
        'liberacao = { modo = "piloto", telefones = ["+5583900000000"] }',
    )
    assert sem_ninguem != texto
    (tmp_path / "politica-liberada.toml").write_text(sem_ninguem, encoding="utf-8")
    _usar_ctn(monkeypatch, _ctn_devendo())

    codigo = main(["disparar", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "enviadas: 0" in saida
    assert "fora da liberação (sem abertura): 1" in saida
    _sem_dado_de_cliente(saida)


def test_disparar_recusa_a_campanha_do_modo_teste(tmp_path, banco, monkeypatch, capsys):
    escrever_politica_de_teste(tmp_path)
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(tmp_path / "politica.toml"))
    chamou_o_ctn = []

    @contextmanager
    def abrir(config):
        chamou_o_ctn.append(True)
        yield _ctn_devendo()

    monkeypatch.setattr("agente.cli._abrir_ctn", abrir)

    codigo = main(["disparar", "--campanha", "teste"])

    saida = capsys.readouterr().out
    assert codigo == 2
    assert "modo teste" in saida
    assert chamou_o_ctn == []
