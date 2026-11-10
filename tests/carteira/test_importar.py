from datetime import date
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from agente.carteira.importar import importar_carteira, normalizar_telefone
from agente.cli import main
from agente.db.modelos import Cliente, Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.politica import Regua
from agente.tipos import FormaHistorico, PagamentoHistorico, StatusCobranca

from ..conftest import alembic_config

CSV_FICTICIO = Path(__file__).parent / "carteira_ficticia.csv"


def _csv(tmp_path, *linhas):
    caminho = tmp_path / "carteira.csv"
    cabecalho = "matricula;nome;telefone;mensalidades_abertas;regua"
    caminho.write_text("\n".join([cabecalho, *linhas]) + "\n", encoding="utf-8")
    return caminho


def _ctn_com_propenso(matricula):
    historico = [
        PagamentoHistorico(date(2026, mes, 10), FormaHistorico.PIX, 3340) for mes in (7, 8, 9)
    ]
    cliente = ClienteCTN(matricula, "Fictício", "+5583999990004", cpf=None)
    return CTNFalso(clientes=[cliente], historicos={matricula: historico}, hoje=date(2026, 10, 22))


def test_importa_as_oito_reguas(sessao, politica):
    relatorio = importar_carteira(CSV_FICTICIO, sessao, politica, CTNFalso(), campanha="teste")

    assert relatorio.criadas == 8
    assert relatorio.atualizadas == 0
    assert relatorio.rejeitadas == []
    reguas = {c.cliente.matricula_ctn: c.regua for c in sessao.scalars(select(Cobranca))}
    assert reguas == {
        "F-0001": Regua.NR,
        "F-0002": Regua.R1,
        "F-0003": Regua.R2,
        "F-0004": Regua.R3,
        "F-0005": Regua.PREVIA,
        "F-0006": Regua.R4,
        "F-0007": Regua.R5,
        "F-0008": Regua.R6,
    }


def test_grava_divida_telefone_e_campanha(sessao, politica):
    importar_carteira(CSV_FICTICIO, sessao, politica, CTNFalso(), campanha="outubro")

    cobranca = sessao.scalars(
        select(Cobranca).join(Cliente).where(Cliente.matricula_ctn == "F-0004")
    ).one()
    assert cobranca.divida_centavos == 10020
    assert cobranca.campanha == "outubro"
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.cliente.telefone == "+5583999990004"


def test_propensao_vem_do_historico_do_ctn(sessao, politica):
    importar_carteira(CSV_FICTICIO, sessao, politica, _ctn_com_propenso("F-0004"), campanha="teste")

    propensos = {
        c.cliente.matricula_ctn: c.propenso_cartao for c in sessao.scalars(select(Cobranca))
    }
    assert propensos["F-0004"] is True
    assert propensos["F-0003"] is False


def test_importar_de_novo_atualiza_sem_duplicar(sessao, politica, tmp_path):
    importar_carteira(CSV_FICTICIO, sessao, politica, CTNFalso(), campanha="teste")
    nova = _csv(tmp_path, "F-0004;Cliente Fictício R3;83999990004;4;")

    relatorio = importar_carteira(nova, sessao, politica, CTNFalso(), campanha="teste")

    assert (relatorio.criadas, relatorio.atualizadas) == (0, 1)
    cobrancas = sessao.scalars(
        select(Cobranca).join(Cliente).where(Cliente.matricula_ctn == "F-0004")
    ).all()
    assert len(cobrancas) == 1
    assert cobrancas[0].regua is Regua.R4
    assert cobrancas[0].divida_centavos == 13360


def test_o_csv_nao_tem_ult_mesref_e_zera_o_que_havia(sessao, politica, tmp_path):
    importar_carteira(
        _csv(tmp_path, "F-0004;Cliente Fictício R3;83999990004;3;"),
        sessao,
        politica,
        CTNFalso(),
        campanha="teste",
    )
    cobranca = sessao.scalars(select(Cobranca)).one()
    assert cobranca.ult_mesref is None
    cobranca.ult_mesref = date(2026, 7, 1)  # veio de uma importação do CTN
    sessao.commit()

    importar_carteira(
        _csv(tmp_path, "F-0004;Cliente Fictício R3;83999990004;4;"),
        sessao,
        politica,
        CTNFalso(),
        campanha="teste",
    )

    assert cobranca.mensalidades_retrato == 4
    assert cobranca.ult_mesref is None


@pytest.mark.parametrize(
    ("linha", "motivo"),
    [
        ("F-0100;Fictício;123;3;", "telefone"),
        ("F-0101;Fictício;83999990101;0;", "mensalidade"),
        ("F-0102;Fictício;83999990102;tres;", "mensalidades_abertas"),
        ("F-0103;Fictício;83999990103;3;R9", "régua"),
        (";Fictício;83999990104;3;", "matrícula"),
    ],
)
def test_linha_invalida_e_rejeitada_com_o_numero_da_linha(
    sessao, politica, tmp_path, linha, motivo
):
    caminho = _csv(tmp_path, "F-0099;Fictício Válido;83999990099;2;", linha)

    relatorio = importar_carteira(caminho, sessao, politica, CTNFalso(), campanha="teste")

    assert relatorio.criadas == 1
    [(numero, texto)] = relatorio.rejeitadas
    assert numero == 3
    assert motivo in texto


def test_coluna_faltando_e_erro(sessao, politica, tmp_path):
    caminho = tmp_path / "carteira.csv"
    caminho.write_text("matricula;nome\nF-1;Fictício\n", encoding="utf-8")
    with pytest.raises(ValueError, match="telefone"):
        importar_carteira(caminho, sessao, politica, CTNFalso(), campanha="teste")


@pytest.mark.parametrize(
    ("bruto", "e164"),
    [
        ("(83) 99999-0001", "+5583999990001"),
        ("+55 83 99999-0001", "+5583999990001"),
        ("5583999990001", "+5583999990001"),
        ("8332220001", "+558332220001"),
    ],
)
def test_normaliza_telefone(bruto, e164):
    assert normalizar_telefone(bruto) == e164


@pytest.mark.parametrize("bruto", ["123", "", "+1 202 555 0100", "83 9999"])
def test_telefone_invalido(bruto):
    assert normalizar_telefone(bruto) is None


def test_comando_importar(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)

    codigo = main(["importar", str(CSV_FICTICIO), "--campanha", "teste"])

    assert codigo == 0
    saida = capsys.readouterr().out
    assert "criadas: 8" in saida
    with criar_sessao(url)() as sessao:
        assert len(sessao.scalars(select(Cobranca)).all()) == 8
