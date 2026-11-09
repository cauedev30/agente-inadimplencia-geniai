from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from alembic import command
from sqlalchemy import select

from agente.carteira.importar_ctn import importar_do_ctn
from agente.cli import main
from agente.db.modelos import Cliente, Cobranca, Conversa, Pagamento
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso, CTNIndisponivel, InadimplenteCTN
from agente.politica import Regua
from agente.tipos import FormaPagamento, StatusCobranca, StatusPagamento

from ..conftest import alembic_config

DIA = date(2026, 11, 9)


def _linha(matricula, meses=1, telefone="+5583900000001", nome="Cliente Fictício", dia=DIA):
    return InadimplenteCTN(
        matricula=matricula,
        nome=nome,
        telefone=telefone,
        meses_atraso=meses,
        meses_ate_corte=6 - meses,
        ult_mesref=date(2026, 11 - meses, 1) if meses else None,
        data_ref=dia,
    )


def _ctn(*linhas):
    return CTNFalso(retrato=linhas)


def _importar(sessao, politica, ctn, simular=False, campanha="nov"):
    return importar_do_ctn(sessao, ctn, politica, DIA, campanha, simular)


def _cobrancas(sessao):
    return sessao.scalars(select(Cobranca).order_by(Cobranca.id)).all()


class _CaiNoMeio(CTNFalso):
    """Entrega `antes` linhas do retrato e depois o CTN cai."""

    def __init__(self, antes, **resto):
        super().__init__(**resto)
        self.antes = antes

    def inadimplentes(self, data_ref):
        def paginas():
            yield from self.retrato[: self.antes]
            raise CTNIndisponivel("caiu no meio")

        return paginas()


def test_grava_cliente_e_cobranca_com_regua_e_divida_do_retrato(sessao, politica):
    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 3), _linha("M-2", 1, "+5583900000002")))

    assert (resumo.criadas, resumo.atualizadas, resumo.encerradas) == (2, 0, 0)
    assert resumo.total_retrato == 2
    primeira = _cobrancas(sessao)[0]
    assert primeira.regua is Regua.R3
    assert primeira.mensalidades_abertas == 3
    assert primeira.divida_centavos == 3 * politica.mensalidade_centavos
    assert primeira.campanha == "nov"
    assert primeira.propenso_cartao is False
    assert primeira.conferida_em is None
    assert primeira.cliente.telefone == "+5583900000001"


def test_energisa_com_zero_meses_fica_de_fora_e_e_contada(sessao, politica):
    resumo = _importar(
        sessao,
        politica,
        _ctn(_linha("M-1", 2), _linha("ENE-1", 0, "+5583900000003"), _linha("ENE-2", 0)),
    )

    assert resumo.criadas == 1
    assert resumo.ignoradas_energisa == 2
    assert resumo.total_retrato == 3
    assert [c.matricula_ctn for c in sessao.scalars(select(Cliente))] == ["M-1"]


def test_linha_sem_telefone_nao_cria_cobranca_mas_conta_como_vista(sessao, politica):
    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 1), _linha("M-2", 1, telefone="")))

    assert resumo.criadas == 1
    assert resumo.sem_telefone == 1
    assert len(_cobrancas(sessao)) == 1


def test_rodar_duas_vezes_nao_duplica(sessao, politica):
    ctn = _ctn(_linha("M-1", 1), _linha("M-2", 2, "+5583900000002"))
    _importar(sessao, politica, ctn)

    resumo = _importar(sessao, politica, ctn)

    assert (resumo.criadas, resumo.atualizadas, resumo.encerradas) == (0, 2, 0)
    assert len(_cobrancas(sessao)) == 2
    assert len(sessao.scalars(select(Cliente)).all()) == 2


def test_matricula_repetida_no_retrato_conta_uma_vez(sessao, politica):
    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 1), _linha("M-1", 1)))

    assert (resumo.criadas, resumo.atualizadas) == (1, 0)
    assert resumo.total_retrato == 2


def test_telefone_com_e_sem_o_9_nao_duplica_o_cliente(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1", 1, "+5583999990001")))

    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 2, "+558399990001")))

    assert resumo.atualizadas == 1
    assert len(sessao.scalars(select(Cliente)).all()) == 1
    assert len(_cobrancas(sessao)) == 1
    assert _cobrancas(sessao)[0].mensalidades_abertas == 2


def test_a_atualizacao_nao_mexe_na_propensao_nem_na_conferencia(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1", 1)))
    cobranca = _cobrancas(sessao)[0]
    cobranca.propenso_cartao = True
    cobranca.conferida_em = datetime(2026, 11, 8, 12, tzinfo=UTC)
    sessao.commit()

    _importar(sessao, politica, _ctn(_linha("M-1", 2)))

    assert cobranca.propenso_cartao is True
    assert cobranca.conferida_em == datetime(2026, 11, 8, 12, tzinfo=UTC)
    assert cobranca.mensalidades_abertas == 2


def test_quem_saiu_do_retrato_e_encerrado_e_quem_ficou_nao(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))

    resumo = _importar(sessao, politica, _ctn(_linha("M-2", 1, "+5583900000002")))

    por_matricula = {c.cliente.matricula_ctn: c for c in _cobrancas(sessao)}
    assert resumo.encerradas == 1
    assert por_matricula["M-1"].status is StatusCobranca.ENCERRADA
    assert por_matricula["M-1"].encerrada_motivo == "saiu_do_retrato"
    assert por_matricula["M-2"].status is StatusCobranca.ATIVA
    assert por_matricula["M-2"].encerrada_motivo is None


def test_quem_voltou_ao_zero_meses_tambem_sai_da_carteira(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))

    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 0), _linha("M-2", 1, "+5583900000002")))

    assert resumo.encerradas == 1
    assert _cobrancas(sessao)[0].status is StatusCobranca.ENCERRADA


def test_so_a_cobranca_ativa_e_encerrada(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    paga = _cobrancas(sessao)[0]
    paga.status = StatusCobranca.PAGA
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-2", 1, "+5583900000002")))

    assert resumo.encerradas == 0
    assert paga.status is StatusCobranca.PAGA
    assert paga.encerrada_motivo is None


def test_retrato_vazio_nao_encerra_ninguem(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))

    resumo = _importar(sessao, politica, _ctn())

    assert resumo.encerradas == 0
    assert resumo.motivo_sem_encerrar == "retrato_vazio"
    assert all(c.status is StatusCobranca.ATIVA for c in _cobrancas(sessao))


def test_retrato_so_com_energisa_tambem_e_suspeito(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1")))

    resumo = _importar(sessao, politica, _ctn(_linha("ENE-1", 0, "+5583900000003")))

    assert resumo.encerradas == 0
    assert resumo.motivo_sem_encerrar == "retrato_vazio"
    assert _cobrancas(sessao)[0].status is StatusCobranca.ATIVA


def test_pagina_com_erro_no_meio_grava_o_que_veio_e_nao_encerra(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("VELHA", 1, "+5583900000009")))
    linhas = [_linha(f"M-{n}", 1, f"+55839000000{n:02d}") for n in range(1, 6)]
    ctn = _CaiNoMeio(antes=3, retrato=linhas)

    resumo = _importar(sessao, politica, ctn)

    assert resumo.motivo_sem_encerrar == "retrato_incompleto"
    assert resumo.criadas == 3
    assert resumo.encerradas == 0
    assert resumo.total_retrato == 3
    status = {c.cliente.matricula_ctn: c.status for c in _cobrancas(sessao)}
    assert status["VELHA"] is StatusCobranca.ATIVA
    assert len(status) == 4


def test_ctn_fora_do_ar_na_primeira_chamada_nao_grava_nem_encerra(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1")))
    ctn = _ctn(_linha("M-1"))
    ctn.fora_do_ar = True

    resumo = _importar(sessao, politica, ctn)

    assert resumo.motivo_sem_encerrar == "retrato_incompleto"
    assert (resumo.criadas, resumo.atualizadas, resumo.encerradas) == (0, 0, 0)
    assert _cobrancas(sessao)[0].status is StatusCobranca.ATIVA


def test_simular_mostra_os_totais_sem_gravar_nada(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    ctn = _ctn(
        _linha("M-2", 3, "+5583900000002"),
        _linha("M-3", 1, "+5583900000003"),
        _linha("ENE-1", 0, "+5583900000004"),
    )

    resumo = _importar(sessao, politica, ctn, simular=True)

    assert (resumo.criadas, resumo.atualizadas, resumo.encerradas) == (1, 1, 1)
    assert resumo.ignoradas_energisa == 1
    assert resumo.total_retrato == 3
    sessao.expire_all()
    assert [c.cliente.matricula_ctn for c in _cobrancas(sessao)] == ["M-1", "M-2"]
    assert all(c.status is StatusCobranca.ATIVA for c in _cobrancas(sessao))
    assert _cobrancas(sessao)[1].mensalidades_abertas == 1


def test_cobranca_com_pix_pendente_nao_e_encerrada(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    com_pix = _cobrancas(sessao)[0]
    sessao.add(
        Pagamento(
            cobranca=com_pix,
            forma=FormaPagamento.PIX,
            valor_centavos=3340,
            referencia="PIX-1",
            status=StatusPagamento.PENDENTE,
        )
    )
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-9", 1, "+5583900000009")))

    assert resumo.encerradas == 1
    assert resumo.preservadas == 1
    assert com_pix.status is StatusCobranca.ATIVA


def test_cobranca_com_conversa_em_andamento_e_encerrada_sem_apagar_a_conversa(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    primeira = _cobrancas(sessao)[0]
    sessao.add(Conversa(cobranca=primeira, chatwoot_conversa_id=77))
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-2", 1, "+5583900000002")))

    assert resumo.encerradas == 1
    assert primeira.status is StatusCobranca.ENCERRADA
    assert len(sessao.scalars(select(Conversa)).all()) == 1


# ---- o comando ----


@pytest.fixture
def banco(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)
    monkeypatch.setenv("AGENTE_CTN_API_KEY", "chave-ficticia")
    return url


def _usar_ctn(monkeypatch, ctn):
    visto = {}

    @contextmanager
    def abrir(config):
        visto["config"] = config
        yield ctn

    monkeypatch.setattr("agente.cli._abrir_ctn", abrir)
    return visto


def test_comando_sem_a_chave_do_ctn_explica_e_nao_grava(banco, monkeypatch, capsys):
    monkeypatch.delenv("AGENTE_CTN_API_KEY")

    codigo = main(["importar-ctn", "--campanha", "nov"])

    assert codigo == 2
    assert "AGENTE_CTN_API_KEY" in capsys.readouterr().out


def test_comando_simular_imprime_so_contagens_e_nao_grava(banco, monkeypatch, capsys):
    ctn = _ctn(_linha("M-1", 2), _linha("ENE-1", 0, "+5583900000003"))
    _usar_ctn(monkeypatch, ctn)

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov", "--simular"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "simulação" in saida
    assert "criadas: 1" in saida
    assert "ignoradas Energisa: 1" in saida
    assert "total do retrato: 2" in saida
    assert "M-1" not in saida and "Fictício" not in saida and "+5583" not in saida
    with criar_sessao(banco)() as sessao:
        assert sessao.scalars(select(Cobranca)).all() == []


def test_comando_grava_e_depois_encerra(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    assert main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"]) == 0
    _usar_ctn(monkeypatch, _ctn(_linha("M-2", 1, "+5583900000002")))
    capsys.readouterr()

    assert main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"]) == 0

    assert "encerradas: 1" in capsys.readouterr().out
    with criar_sessao(banco)() as sessao:
        status = sorted(c.status for c in sessao.scalars(select(Cobranca)))
        assert status == [StatusCobranca.ATIVA, StatusCobranca.ENCERRADA]


def test_comando_com_retrato_incompleto_avisa_e_sai_com_1(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _CaiNoMeio(antes=1, retrato=[_linha("M-1"), _linha("M-2")]))

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 1
    assert "retrato incompleto: nada foi encerrado" in saida
    assert "criadas: 1" in saida


def test_comando_com_retrato_vazio_avisa_que_nao_encerrou(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn())

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"])

    assert codigo == 0
    assert "retrato sem inadimplentes: nada foi encerrado" in capsys.readouterr().out


def test_comando_sem_data_usa_o_dia_de_hoje_no_fuso_da_janela(banco, monkeypatch):
    monkeypatch.setattr("agente.cli._agora", lambda: datetime(2026, 11, 10, 1, 0, tzinfo=UTC))
    pedidos = []

    class Espiao(CTNFalso):
        def inadimplentes(self, data_ref):
            pedidos.append(data_ref)
            return iter([])

    _usar_ctn(monkeypatch, Espiao())

    main(["importar-ctn", "--campanha", "nov"])

    # 01:00 UTC de 10/11 ainda é 22:00 de 09/11 em Fortaleza.
    assert pedidos == [date(2026, 11, 9)]
