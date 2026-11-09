from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import event, func, select, update

from agente.carteira import importar_ctn
from agente.carteira.importar_ctn import ResumoImportacao, importar_do_ctn
from agente.cli import main
from agente.db.modelos import Cliente, Cobranca, Conversa, Pagamento
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso, CTNIndisponivel, InadimplenteCTN
from agente.politica import Regua
from agente.tipos import FormaPagamento, StatusCobranca, StatusPagamento

from ..conftest import alembic_config

DIA = date(2026, 11, 9)
# 12h em Fortaleza (a janela da política): o dia do negócio é 09/11.
AGORA = datetime(2026, 11, 9, 15, tzinfo=UTC)


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


def _importar(sessao, politica, ctn, simular=False, campanha="nov", dia=DIA, agora=AGORA):
    return importar_do_ctn(sessao, ctn, politica, dia, campanha, simular, agora)


def _cobrancas(sessao):
    return sessao.scalars(select(Cobranca).order_by(Cobranca.id)).all()


class _CaiNoMeio(CTNFalso):
    """Entrega `antes` linhas do retrato e depois o CTN cai."""

    def __init__(self, antes, **resto):
        super().__init__(**resto)
        self.antes = antes

    def inadimplentes(self, data_ref, invalidas=None):
        def paginas():
            yield from self.retrato[: self.antes]
            raise CTNIndisponivel("caiu no meio")

        return paginas()


class _SemFiltroDeData(CTNFalso):
    """Devolve o retrato todo, de qualquer data: um CTN que ignorasse o `de`/`ate`."""

    def inadimplentes(self, data_ref, invalidas=None):
        return iter(self.retrato)


class _ComLinhasInvalidas(CTNFalso):
    """Entrega as linhas boas e avisa de linhas malformadas (matrícula legível ou None)."""

    def __init__(self, ruins, **resto):
        super().__init__(**resto)
        self.ruins = ruins

    def inadimplentes(self, data_ref, invalidas=None):
        invalidas.extend(self.ruins)
        return iter([x for x in self.retrato if x.data_ref == data_ref])


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
    # Conferida ontem: o retrato de hoje vale e muda os números.
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.divida_centavos == 2 * politica.mensalidade_centavos


def test_cobranca_conferida_hoje_mantem_os_numeros_da_conferencia(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1", 1)))
    cobranca = _cobrancas(sessao)[0]
    cobranca.conferida_em = datetime(2026, 11, 9, 11, tzinfo=UTC)
    cobranca.mensalidades_abertas = 3
    cobranca.divida_centavos = 9999
    cobranca.regua = Regua.R4
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-1", 2, nome="Nome Novo")))

    assert resumo.atualizadas == 1
    assert (cobranca.mensalidades_abertas, cobranca.divida_centavos) == (3, 9999)
    assert cobranca.regua is Regua.R4
    # O resto da atualização segue valendo.
    assert cobranca.cliente.nome == "Nome Novo"


def test_conferida_hoje_vale_pelo_dia_do_negocio_e_nao_pelo_utc(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1", 1)))
    cobranca = _cobrancas(sessao)[0]
    # 02:00 UTC de 10/11 ainda é 23:00 de 09/11 em Fortaleza: o mesmo dia de AGORA.
    cobranca.conferida_em = datetime(2026, 11, 10, 2, tzinfo=UTC)
    sessao.commit()

    _importar(sessao, politica, _ctn(_linha("M-1", 2)))

    assert cobranca.mensalidades_abertas == 1


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


def test_retrato_que_cai_no_meio_nao_grava_nada_nem_encerra(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("VELHA", 1, "+5583900000009")))
    linhas = [_linha(f"M-{n}", 1, f"+55839000000{n:02d}") for n in range(1, 6)]
    ctn = _CaiNoMeio(antes=3, retrato=linhas)

    resumo = _importar(sessao, politica, ctn)

    assert resumo.motivo_sem_encerrar == "retrato_incompleto"
    assert resumo.completo is False
    assert (resumo.criadas, resumo.atualizadas, resumo.encerradas) == (0, 0, 0)
    status = {c.cliente.matricula_ctn: c.status for c in _cobrancas(sessao)}
    assert status == {"VELHA": StatusCobranca.ATIVA}
    assert len(sessao.scalars(select(Cliente)).all()) == 1


def test_o_retrato_e_lido_inteiro_antes_de_qualquer_escrita(sessao, politica):
    contagens = []

    class Espiao(CTNFalso):
        def inadimplentes(self, data_ref, invalidas=None):
            def linhas():
                for linha in self.retrato:
                    contagens.append(sessao.scalar(select(func.count(Cliente.id))))
                    yield linha

            return linhas()

    linhas = [_linha(f"M-{n}", 1, f"+55839000000{n:02d}") for n in range(5)]
    _importar(sessao, politica, Espiao(retrato=linhas))

    assert contagens == [0, 0, 0, 0, 0]
    assert len(sessao.scalars(select(Cliente)).all()) == 5


def test_grava_em_lotes_com_um_commit_por_lote(sessao, politica, monkeypatch):
    monkeypatch.setattr(importar_ctn, "PAGINA", 2)
    commits = []
    event.listen(sessao, "after_commit", lambda _s: commits.append(1))
    linhas = [_linha(f"M-{n}", 1, f"+55839000000{n:02d}") for n in range(5)]

    _importar(sessao, politica, _ctn(*linhas))

    # 3 lotes (2 + 2 + 1) e o commit final.
    assert len(commits) == 4
    assert len(_cobrancas(sessao)) == 5


# ---- a campanha, a data do retrato e o teto do encerramento ----


def test_so_encerra_ativa_da_mesma_campanha(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1")), campanha="outra")
    _importar(sessao, politica, _ctn(_linha("M-2", 1, "+5583900000002")), campanha="nov")

    resumo = _importar(sessao, politica, _ctn(_linha("M-3", 1, "+5583900000003")), campanha="nov")

    por_matricula = {c.cliente.matricula_ctn: c.status for c in _cobrancas(sessao)}
    assert resumo.encerradas == 1
    assert por_matricula["M-1"] is StatusCobranca.ATIVA
    assert por_matricula["M-2"] is StatusCobranca.ENCERRADA


def test_linha_de_outra_data_nao_entra_e_deixa_o_retrato_suspeito(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("VELHA", 1, "+5583900000009")))
    ctn = _SemFiltroDeData(
        retrato=[
            _linha("M-1"),
            _linha("M-2", 1, "+5583900000002", dia=DIA - timedelta(days=1)),
        ]
    )

    resumo = _importar(sessao, politica, ctn)

    assert resumo.data_ref_divergente == 1
    assert resumo.criadas == 1
    assert resumo.motivo_sem_encerrar == "retrato_suspeito"
    assert resumo.encerradas == 0
    matriculas = {c.cliente.matricula_ctn for c in _cobrancas(sessao)}
    assert matriculas == {"VELHA", "M-1"}


def test_retrato_sem_nenhuma_linha_da_data_pedida_e_suspeito_e_nao_encerra(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("VELHA", 1, "+5583900000009")))
    ontem = DIA - timedelta(days=1)
    ctn = _SemFiltroDeData(retrato=[_linha("M-1", dia=ontem), _linha("M-2", dia=ontem)])

    resumo = _importar(sessao, politica, ctn)

    assert resumo.motivo_sem_encerrar == "retrato_suspeito"
    assert resumo.data_ref_divergente == 2
    assert (resumo.criadas, resumo.encerradas) == (0, 0)
    assert _cobrancas(sessao)[0].status is StatusCobranca.ATIVA


def _carteira_de(sessao, politica, quantas):
    linhas = [_linha(f"M-{n:02d}", 1, f"+55839000{n:05d}") for n in range(quantas)]
    _importar(sessao, politica, _ctn(*linhas))
    return linhas


def test_encerramento_acima_de_25_por_cento_das_ativas_nao_roda(sessao, politica):
    linhas = _carteira_de(sessao, politica, 20)

    resumo = _importar(sessao, politica, _ctn(*linhas[:14]))

    assert resumo.motivo_sem_encerrar == "encerramento_excessivo"
    assert resumo.encerradas == 0
    assert all(c.status is StatusCobranca.ATIVA for c in _cobrancas(sessao))


def test_encerramento_de_exatamente_25_por_cento_roda(sessao, politica):
    linhas = _carteira_de(sessao, politica, 20)

    resumo = _importar(sessao, politica, _ctn(*linhas[:15]))

    assert resumo.motivo_sem_encerrar is None
    assert resumo.encerradas == 5


def test_o_teto_so_vale_com_ao_menos_20_ativas(sessao, politica):
    linhas = _carteira_de(sessao, politica, 19)

    resumo = _importar(sessao, politica, _ctn(*linhas[:2]))

    assert resumo.encerradas == 17
    assert resumo.motivo_sem_encerrar is None


def test_o_teto_conta_so_as_ativas_da_campanha(sessao, politica):
    _importar(
        sessao,
        politica,
        _ctn(*[_linha(f"X-{n}", 1, f"+55839111{n:05d}") for n in range(30)]),
        campanha="outra",
    )
    linhas = _carteira_de(sessao, politica, 4)

    resumo = _importar(sessao, politica, _ctn(*linhas[:3]))

    # 1 de 4 ativas da campanha (25%) com 34 no total: o teto não vale (menos de 20 na campanha).
    assert resumo.encerradas == 1


# ---- linhas malformadas, telefone repetido e Pix ----


def test_linha_malformada_com_matricula_legivel_conta_como_vista(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    ctn = _ComLinhasInvalidas(["M-1", None], retrato=[_linha("M-2", 1, "+5583900000002")])

    resumo = _importar(sessao, politica, ctn)

    assert resumo.linhas_invalidas == 2
    assert resumo.encerradas == 0
    assert all(c.status is StatusCobranca.ATIVA for c in _cobrancas(sessao))


def test_retrato_so_de_linhas_malformadas_e_suspeito(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))

    resumo = _importar(sessao, politica, _ComLinhasInvalidas(["M-1"], retrato=[]))

    assert resumo.motivo_sem_encerrar == "retrato_suspeito"
    assert resumo.encerradas == 0


def test_conta_os_telefones_compartilhados_por_mais_de_uma_matricula(sessao, politica):
    resumo = _importar(
        sessao,
        politica,
        _ctn(
            _linha("M-1", 1, "+5583900000001"),
            _linha("M-2", 1, "+5583900000001"),
            _linha("M-3", 1, "+5583900000001"),
            _linha("M-4", 1, "+5583900000004"),
            _linha("M-5", 1, "+5583900000005"),
            _linha("M-6", 1, "+5583900000005"),
        ),
    )

    assert resumo.telefones_compartilhados == 2
    assert resumo.inadimplentes == 6


def test_inadimplentes_distintos_contam_quem_nao_tem_telefone_e_nao_a_energisa(sessao, politica):
    resumo = _importar(
        sessao,
        politica,
        _ctn(
            _linha("M-1"),
            _linha("M-1"),
            _linha("M-2", 1, telefone=""),
            _linha("ENE-1", 0, "+5583900000003"),
        ),
    )

    assert resumo.inadimplentes == 2
    assert resumo.criadas + resumo.atualizadas + resumo.sem_telefone == 2


def _com_ids_das_ativas(sessao):
    return importar_ctn._ativas_da_campanha(sessao, "nov")


def test_o_encerramento_e_condicional_e_nao_toca_na_cobranca_que_pagou_depois_da_leitura(
    sessao, politica
):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    ativas = _com_ids_das_ativas(sessao)
    primeira = _cobrancas(sessao)[0]
    # Entre a leitura das ativas e a escrita, a confirmação do pagamento fecha a cobrança.
    sessao.execute(
        update(Cobranca).where(Cobranca.id == primeira.id).values(status=StatusCobranca.PAGA)
    )
    resumo = ResumoImportacao()

    importar_ctn._encerrar_quem_saiu(sessao, ativas, set(), AGORA, resumo)

    por_matricula = {c.cliente.matricula_ctn: c.status for c in _cobrancas(sessao)}
    assert por_matricula == {"M-1": StatusCobranca.PAGA, "M-2": StatusCobranca.ENCERRADA}
    assert resumo.encerradas == 1


def test_o_encerramento_nao_toca_na_cobranca_que_ganhou_pix_depois_da_leitura(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    ativas = _com_ids_das_ativas(sessao)
    primeira = _cobrancas(sessao)[0]
    sessao.add(
        Pagamento(
            cobranca=primeira,
            forma=FormaPagamento.PIX,
            valor_centavos=3340,
            referencia="PIX-2",
            status=StatusPagamento.PENDENTE,
        )
    )
    sessao.flush()
    resumo = ResumoImportacao()

    importar_ctn._encerrar_quem_saiu(sessao, ativas, set(), AGORA, resumo)

    assert primeira.status is StatusCobranca.ATIVA
    assert resumo.encerradas == 1


# ---- a cobrança que volta ao retrato ----


def _encerrar_m1_e_voltar(sessao, politica, dias_depois):
    _importar(sessao, politica, _ctn(_linha("M-1"), _linha("M-2", 1, "+5583900000002")))
    primeira = _cobrancas(sessao)[0]
    sessao.add(Conversa(cobranca=primeira, chatwoot_conversa_id=77))
    sessao.commit()
    _importar(sessao, politica, _ctn(_linha("M-2", 1, "+5583900000002")))
    assert primeira.status is StatusCobranca.ENCERRADA
    assert primeira.encerrada_em == AGORA
    volta = AGORA + timedelta(days=dias_depois)
    resumo = _importar(
        sessao,
        politica,
        _ctn(
            _linha("M-1", 2, dia=volta.date()), _linha("M-2", 1, "+5583900000002", dia=volta.date())
        ),
        dia=volta.date(),
        agora=volta,
    )
    return primeira, resumo


def test_quem_volta_ao_retrato_em_ate_3_dias_reativa_a_mesma_cobranca(sessao, politica):
    primeira, resumo = _encerrar_m1_e_voltar(sessao, politica, 3)

    assert (resumo.reativadas, resumo.criadas) == (1, 0)
    assert primeira.status is StatusCobranca.ATIVA
    assert primeira.encerrada_motivo is None and primeira.encerrada_em is None
    assert primeira.mensalidades_abertas == 2
    assert len(_cobrancas(sessao)) == 2
    conversas = sessao.scalars(select(Conversa)).all()
    assert [c.cobranca_id for c in conversas] == [primeira.id]
    assert conversas[0].chatwoot_conversa_id == 77


def test_quem_volta_depois_de_3_dias_ganha_uma_cobranca_nova(sessao, politica):
    primeira, resumo = _encerrar_m1_e_voltar(sessao, politica, 4)

    assert (resumo.reativadas, resumo.criadas) == (0, 1)
    assert primeira.status is StatusCobranca.ENCERRADA
    assert len(_cobrancas(sessao)) == 3


def test_cobranca_encerrada_por_outro_motivo_nunca_e_reativada(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1")))
    primeira = _cobrancas(sessao)[0]
    primeira.status = StatusCobranca.ENCERRADA
    primeira.encerrada_motivo = "pedido_do_cliente"
    primeira.encerrada_em = AGORA
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-1")))

    assert (resumo.reativadas, resumo.criadas) == (0, 1)
    assert primeira.status is StatusCobranca.ENCERRADA


def test_cobranca_paga_nunca_e_reativada(sessao, politica):
    _importar(sessao, politica, _ctn(_linha("M-1")))
    primeira = _cobrancas(sessao)[0]
    primeira.status = StatusCobranca.PAGA
    primeira.encerrada_motivo = "saiu_do_retrato"
    primeira.encerrada_em = AGORA
    sessao.commit()

    resumo = _importar(sessao, politica, _ctn(_linha("M-1")))

    assert (resumo.reativadas, resumo.criadas) == (0, 1)
    assert primeira.status is StatusCobranca.PAGA


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
    assert "retrato incompleto: nada foi gravado nem encerrado" in saida
    assert "motivo de não encerrar: retrato_incompleto" in saida
    with criar_sessao(banco)() as sessao:
        assert sessao.scalars(select(Cobranca)).all() == []


def test_comando_com_retrato_vazio_avisa_o_motivo_e_sai_com_3(banco, monkeypatch, capsys):
    _usar_ctn(monkeypatch, _ctn())

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 3
    assert "motivo de não encerrar: retrato_vazio" in saida
    assert "retrato sem inadimplentes: nada foi encerrado" in saida


def test_comando_com_retrato_suspeito_sai_com_3(banco, monkeypatch, capsys):
    ontem = DIA - timedelta(days=1)
    _usar_ctn(monkeypatch, _SemFiltroDeData(retrato=[_linha("M-1", dia=ontem)]))

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 3
    assert "motivo de não encerrar: retrato_suspeito" in saida
    assert "linhas de outra data: 1" in saida


def test_comando_imprime_o_total_distinto_os_invalidos_e_os_telefones_repetidos(
    banco, monkeypatch, capsys
):
    ctn = _ComLinhasInvalidas(
        [None],
        retrato=[
            _linha("M-1", 1, "+5583900000001"),
            _linha("M-2", 1, "+5583900000001"),
            _linha("M-3", 2, "+5583900000003"),
            _linha("M-3", 2, "+5583900000003"),
        ],
    )
    _usar_ctn(monkeypatch, ctn)

    codigo = main(["importar-ctn", "--data", "2026-11-09", "--campanha", "nov"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "inadimplentes distintos (1 mês ou mais): 3" in saida
    assert "linhas inválidas: 1" in saida
    assert "telefones compartilhados por mais de uma matrícula: 1" in saida
    assert "M-1" not in saida and "Fictício" not in saida and "+5583" not in saida


def test_comando_sem_data_usa_o_dia_de_hoje_no_fuso_da_janela(banco, monkeypatch):
    monkeypatch.setattr("agente.cli._agora", lambda: datetime(2026, 11, 10, 1, 0, tzinfo=UTC))
    pedidos = []

    class Espiao(CTNFalso):
        def inadimplentes(self, data_ref, invalidas=None):
            pedidos.append(data_ref)
            return iter([])

    _usar_ctn(monkeypatch, Espiao())

    main(["importar-ctn", "--campanha", "nov"])

    # 01:00 UTC de 10/11 ainda é 22:00 de 09/11 em Fortaleza.
    assert pedidos == [date(2026, 11, 9)]
