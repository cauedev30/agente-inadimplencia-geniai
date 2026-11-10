"""A conferência no contato (spec do CTN real, seção 4): os meses e o valor exatos de um cliente."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agente.carteira.conferir import (
    MOTIVO_DADO_SUSPEITO,
    MOTIVO_NAO_COBRE_O_RETRATO,
    MOTIVO_SEM_LANCAMENTOS,
    TETO_SANITARIO_MENSALIDADE_CENTAVOS,
    Conferencia,
    ResultadoConferencia,
    conferir_antes_do_envio,
    conferir_cobranca,
    precisa_conferir,
)
from agente.db.modelos import Cliente, Cobranca
from agente.integracoes.ctn import CTNFalso, MensalidadeAberta
from agente.politica import Regua
from agente.tipos import FormaHistorico, PagamentoHistorico, StatusCobranca

JP = ZoneInfo("America/Fortaleza")
AGORA = datetime(2026, 11, 9, 10, tzinfo=JP)
MATRICULA = "M-0001"


class CTNEspiao(CTNFalso):
    """Conta as chamadas de leitura de cada tipo."""

    def __init__(self, **resto):
        super().__init__(**resto)
        self.chamadas = []

    def mensalidades_em_aberto(self, matricula):
        self.chamadas.append("mensalidades")
        return super().mensalidades_em_aberto(matricula)

    def historico_pagamentos(self, matricula, meses):
        self.chamadas.append("historico")
        return super().historico_pagamentos(matricula, meses)


def _cobranca(sessao, meses=3, divida=10020, regua=Regua.R3, matricula=MATRICULA, ult_mesref=None):
    cliente = Cliente(matricula_ctn=matricula, nome="Cliente Fictício", telefone="+5583900000001")
    cobranca = Cobranca(
        cliente=cliente,
        regua=regua,
        mensalidades_abertas=meses,
        divida_centavos=divida,
        propenso_cartao=False,
        campanha="nov",
        ult_mesref=ult_mesref,
    )
    sessao.add(cobranca)
    sessao.commit()
    return cobranca


def _abertas(*itens):
    return [MensalidadeAberta(mes, valor) for mes, valor in itens]


def _ctn(abertas, historico=None, matricula=MATRICULA, cls=CTNEspiao, rejeitadas=None):
    # `rejeitadas`: quantos lançamentos rejeitados o CTN devolveu (sem ele, são as abertas).
    return cls(
        mensalidades_abertas={matricula: abertas},
        historicos={matricula: historico or []},
        hoje=date(2026, 11, 9),
        rejeitadas_lidas={matricula: rejeitadas} if rejeitadas is not None else None,
    )


def _pix(*datas):
    return [
        PagamentoHistorico(data=d, forma=FormaHistorico.PIX, valor_centavos=3340) for d in datas
    ]


def test_valor_diferente_do_retrato_corrige_a_divida_e_os_meses(sessao, politica):
    cobranca = _cobranca(sessao, meses=3, divida=10020, regua=Regua.R3)
    ctn = _ctn(_abertas(("2026/09", 3340), ("2026/10", 3500)))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia == Conferencia(
        meses_em_aberto=["2026/09", "2026/10"], divida_centavos=6840, propenso=False, conferida=True
    )
    assert cobranca.meses_em_aberto == ["2026/09", "2026/10"]
    assert cobranca.divida_centavos == 6840
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.conferida_em == AGORA
    # A régua derivada do número de meses acompanha (R3 -> R2).
    assert cobranca.regua is Regua.R2


def test_meses_gravados_em_ordem(sessao, politica):
    cobranca = _cobranca(sessao, meses=2, divida=6680, regua=Regua.R2)
    ctn = _ctn(_abertas(("2026/10", 3340), ("2026/08", 3340)))

    conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert cobranca.meses_em_aberto == ["2026/08", "2026/10"]


def test_regua_informada_pela_lista_nao_e_trocada(sessao, politica):
    # Régua que a derivação por meses não daria (a lista informou NR): a conferência respeita.
    cobranca = _cobranca(sessao, meses=3, divida=10020, regua=Regua.NR)
    ctn = _ctn(_abertas(("2026/09", 3340), ("2026/10", 3340)))

    conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert cobranca.regua is Regua.NR
    assert cobranca.mensalidades_abertas == 2


def test_ctn_fora_mantem_o_retrato_e_nao_marca_conferida(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/10", 3340)))
    ctn.fora_do_ar = True

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo is None
    assert conferencia.divida_centavos == 10020
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.divida_centavos == 10020
    assert cobranca.mensalidades_abertas == 3


def test_propenso_quando_o_historico_tem_tres_pix_seguidos(sessao, politica):
    cobranca = _cobranca(sessao)
    historico = _pix(date(2026, 8, 10), date(2026, 9, 11), date(2026, 10, 12))
    ctn = _ctn(_abertas(("2026/11", 3340)), historico)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.propenso is True
    assert cobranca.propenso_cartao is True


def test_propenso_some_se_o_historico_nao_sustenta(sessao, politica):
    cobranca = _cobranca(sessao)
    cobranca.propenso_cartao = True
    sessao.commit()
    ctn = _ctn(_abertas(("2026/11", 3340)), _pix(date(2026, 10, 12)))

    conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert cobranca.propenso_cartao is False


def test_rejeitadas_todas_pagas_e_quitada_e_nao_mexe_na_divida(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn([], rejeitadas=3)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert conferencia.meses_em_aberto == []
    assert conferencia.quitada_no_ctn is True
    assert conferencia.motivo is None
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.encerrada_motivo is None
    assert cobranca.meses_em_aberto == []
    assert cobranca.conferida_em == AGORA
    # Os valores do retrato ficam: a decisão é de quem chamou.
    assert cobranca.divida_centavos == 10020
    assert cobranca.mensalidades_abertas == 3
    # O histórico não é lido quando não há dívida a cobrar.
    assert ctn.chamadas == ["mensalidades"]


def test_rejeitadas_pagas_que_nao_cobrem_o_retrato_nao_quitam(sessao, politica):
    # O retrato diz 5 meses e o CTN só tem 3 lançamentos rejeitados, todos pagos: sobram 2 meses de
    # que a conferência não sabe o mês nem o valor. Não é "quitada" e nada é gravado.
    cobranca = _cobranca(sessao, meses=5, divida=16700, regua=Regua.R3)
    ctn = _ctn([], rejeitadas=3)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.quitada_no_ctn is False
    assert conferencia.motivo == MOTIVO_NAO_COBRE_O_RETRATO
    assert conferencia.divida_centavos == 16700  # o do retrato, que o chamador não cita
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.mensalidades_abertas == 5


def test_o_retrato_guardado_vale_mesmo_depois_de_a_conferencia_mexer_nas_mensalidades(
    sessao, politica
):
    # A conferência de ontem baixou `mensalidades_abertas` para 3; o retrato continua sendo 5.
    cobranca = _cobranca(sessao, meses=3, divida=10020)
    cobranca.mensalidades_retrato = 5
    sessao.commit()
    ctn = _ctn([], rejeitadas=3)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is False
    assert conferencia.motivo == MOTIVO_NAO_COBRE_O_RETRATO


def test_rejeitadas_pagas_que_cobrem_o_retrato_com_folga_quitam(sessao, politica):
    # Há mais rejeitadas lidas que meses no retrato (as antigas, já pagas): cobre.
    cobranca = _cobranca(sessao, meses=2, divida=6680, regua=Regua.R2)
    cobranca.mensalidades_retrato = 2
    sessao.commit()

    conferencia = conferir_cobranca(sessao, _ctn([], rejeitadas=7), politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is True


def test_antes_do_envio_trata_o_retrato_nao_coberto_como_dado_suspeito(sessao, politica):
    cobranca = _cobranca(sessao, meses=5, divida=16700)
    ctn = _ctn([], rejeitadas=3)

    resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)

    assert resultado is ResultadoConferencia.DADO_SUSPEITO
    assert cobranca.conferida_em is None  # tenta de novo no próximo envio


def test_matricula_desconhecida_nao_e_quitada_e_nao_marca_conferida(sessao, politica):
    # Nenhuma rejeitada veio (o CTN não conhece a matrícula, ou ela não casa com a dos lançamentos)
    # e o retrato diz que há 3 meses em atraso: contradição, não "quitada".
    cobranca = _cobranca(sessao, matricula="M-9999")
    ctn = CTNEspiao(hoje=date(2026, 11, 9))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.quitada_no_ctn is False
    assert conferencia.motivo == MOTIVO_SEM_LANCAMENTOS
    assert conferencia.divida_centavos == 10020
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.divida_centavos == 10020
    # O histórico não é lido quando não há dívida a cobrar.
    assert ctn.chamadas == ["mensalidades"]


@pytest.mark.parametrize(
    ("abertas", "motivo_esperado"),
    [
        # mês repetido entre as abertas
        ([("2026/09", 3340), ("2026/09", 3340)], MOTIVO_DADO_SUSPEITO),
        # valor zero e valor negativo
        ([("2026/09", 0), ("2026/10", 3340)], MOTIVO_DADO_SUSPEITO),
        ([("2026/09", -3340)], MOTIVO_DADO_SUSPEITO),
        # acima do teto sanitário
        ([("2026/10", TETO_SANITARIO_MENSALIDADE_CENTAVOS + 1)], MOTIVO_DADO_SUSPEITO),
        # mais meses abertos que o retrato (3) + 1
        ([(f"2026/0{n}", 3340) for n in range(1, 6)], MOTIVO_DADO_SUSPEITO),
    ],
)
def test_dado_suspeito_nao_marca_conferida_nem_grava(sessao, politica, abertas, motivo_esperado):
    cobranca = _cobranca(sessao, meses=3)
    ctn = _ctn(_abertas(*abertas))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == motivo_esperado
    assert conferencia.divida_centavos == 10020
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.divida_centavos == 10020
    assert cobranca.mensalidades_abertas == 3


def test_um_mes_a_mais_que_o_retrato_e_o_teto_exato_ainda_conferem(sessao, politica):
    cobranca = _cobranca(sessao, meses=1, divida=3340, regua=Regua.R1)
    ctn = _ctn(_abertas(("2026/09", TETO_SANITARIO_MENSALIDADE_CENTAVOS), ("2026/10", 1000)))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert conferencia.divida_centavos == TETO_SANITARIO_MENSALIDADE_CENTAVOS + 1000


def test_mensalidade_de_dez_a_noventa_e_nove_reais_confere(sessao, politica):
    # As mensalidades reais vão de R$ 10 a R$ 99: o valor fixo da política é só uma estimativa.
    cobranca = _cobranca(sessao, meses=2, divida=6680, regua=Regua.R2)
    ctn = _ctn(_abertas(("2026/09", 1000), ("2026/10", 9900)))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert cobranca.divida_centavos == 10900


def test_idempotente(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/09", 3340), ("2026/10", 3340)))

    primeira = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)
    segunda = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert primeira == segunda
    assert cobranca.divida_centavos == 6680
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.regua is Regua.R2


def test_cobranca_que_nao_esta_ativa_nao_e_alterada(sessao, politica):
    cobranca = _cobranca(sessao)
    cobranca.status = StatusCobranca.PAGA
    sessao.commit()
    ctn = _ctn(_abertas(("2026/10", 3340)))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert cobranca.conferida_em is None
    assert cobranca.divida_centavos == 10020


def test_precisa_conferir_so_se_nunca_ou_de_outro_dia(sessao, politica):
    cobranca = _cobranca(sessao)
    assert precisa_conferir(cobranca, politica, AGORA) is True

    cobranca.conferida_em = AGORA - timedelta(hours=2)
    assert precisa_conferir(cobranca, politica, AGORA) is False

    cobranca.conferida_em = AGORA - timedelta(days=1)
    assert precisa_conferir(cobranca, politica, AGORA) is True


def test_o_dia_e_o_do_fuso_da_politica(sessao, politica):
    cobranca = _cobranca(sessao)
    # 02h UTC de 9/11 é 23h de 8/11 em Fortaleza (UTC-3).
    cobranca.conferida_em = datetime(2026, 11, 9, 2, tzinfo=UTC)
    # 02h59 UTC ainda é o dia 8 local: não reconfere. 03h UTC já é o dia 9 local: reconfere.
    assert precisa_conferir(cobranca, politica, datetime(2026, 11, 9, 2, 59, tzinfo=UTC)) is False
    assert precisa_conferir(cobranca, politica, datetime(2026, 11, 9, 3, tzinfo=UTC)) is True


def test_antes_do_envio_confere_grava_e_libera(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/10", 3340)))

    resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)

    assert resultado is ResultadoConferencia.CONFERIDA
    assert cobranca.divida_centavos == 3340
    assert ctn.chamadas == ["mensalidades", "historico"]


def test_antes_do_envio_so_confere_uma_vez_por_dia(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/10", 3340)))

    conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)
    conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA + timedelta(hours=3))

    assert ctn.chamadas == ["mensalidades", "historico"]

    conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA + timedelta(days=1))

    assert ctn.chamadas == ["mensalidades", "historico"] * 2


def test_antes_do_envio_marca_a_quitada_no_ctn_no_mesmo_dia(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn([], rejeitadas=3)

    primeira = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)
    # Mesmo dia: não chama o CTN de novo e continua quitada.
    segunda = conferir_antes_do_envio(
        sessao, ctn, politica, cobranca.id, AGORA + timedelta(hours=1)
    )

    assert primeira is segunda is ResultadoConferencia.QUITADA
    assert ctn.chamadas == ["mensalidades"]


def test_antes_do_envio_distingue_sem_lancamentos_e_dado_suspeito(sessao, politica):
    sem = _cobranca(sessao, matricula="M-0002")
    suspeita = _cobranca(sessao, matricula="M-0003")
    ctn = CTNEspiao(
        mensalidades_abertas={"M-0003": _abertas(("2026/10", 0))}, hoje=date(2026, 11, 9)
    )

    assert (
        conferir_antes_do_envio(sessao, ctn, politica, sem.id, AGORA)
        is ResultadoConferencia.SEM_LANCAMENTOS
    )
    assert (
        conferir_antes_do_envio(sessao, ctn, politica, suspeita.id, AGORA)
        is ResultadoConferencia.DADO_SUSPEITO
    )
    # Nenhuma das duas fica marcada: a próxima rodada confere de novo.
    assert sem.conferida_em is None
    assert suspeita.conferida_em is None


def test_quitada_de_ontem_nao_vale_se_o_ctn_cai_hoje(sessao, politica):
    cobranca = _cobranca(sessao)
    cobranca.conferida_em = AGORA - timedelta(days=1)
    cobranca.meses_em_aberto = []
    sessao.commit()
    ctn = _ctn([])
    ctn.fora_do_ar = True

    # Sem conferência de hoje: não é quitada (ontem não vale) e também não está conferida.
    resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)

    assert resultado is ResultadoConferencia.NAO_CONFERIDA


def test_antes_do_envio_com_ctn_fora_diz_que_nao_conferiu(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/10", 3340)))
    ctn.fora_do_ar = True

    resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)

    assert resultado is ResultadoConferencia.NAO_CONFERIDA
    assert cobranca.conferida_em is None
    assert cobranca.divida_centavos == 10020


# ---- o `ult_mesref` do retrato só desempata (D55) ----

ULT = date(2026, 7, 1)  # o retrato conta como atraso os meses de 2026/08 em diante
VELHAS = _abertas(("2025/03", 3340), ("2025/09", 3340), ("2026/05", 3340), ("2026/07", 3340))
NOVAS = _abertas(("2026/08", 3340), ("2026/09", 3340), ("2026/10", 3340))


def _em_ordem(*grupos):
    return sorted([m for grupo in grupos for m in grupo], key=lambda m: m.mes_ano)


@pytest.mark.parametrize(
    "meses",
    [
        ("2026/05", "2026/06", "2026/07"),  # todas até o mês do `ult_mesref`: o filtro zeraria tudo
        ("2026/06", "2026/07", "2026/08"),  # o filtro deixaria 1 de 3: dívida menor que a do CTN
    ],
)
def test_abertas_iguais_ao_retrato_nao_sao_filtradas_mesmo_com_ult_mesref(sessao, politica, meses):
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn(_abertas(*[(mes, 3340) for mes in meses]))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia == Conferencia(
        meses_em_aberto=list(meses), divida_centavos=10020, propenso=False, conferida=True
    )
    assert cobranca.mensalidades_abertas == 3
    assert cobranca.divida_centavos == 10020
    assert cobranca.ult_mesref == ULT  # a conferência não mexe nele


def test_suspeitas_com_o_filtrado_igual_ao_retrato_desempata_e_confere(sessao, politica):
    # 7 abertas contra um retrato de 3; depois do `ult_mesref` há exatamente 3.
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn(_em_ordem(VELHAS, NOVAS))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia == Conferencia(
        meses_em_aberto=["2026/08", "2026/09", "2026/10"],
        divida_centavos=10020,
        propenso=False,
        conferida=True,
    )
    assert cobranca.meses_em_aberto == ["2026/08", "2026/09", "2026/10"]
    assert cobranca.mensalidades_abertas == 3
    assert cobranca.divida_centavos == 10020
    assert cobranca.conferida_em == AGORA
    assert cobranca.ult_mesref == ULT


@pytest.mark.parametrize(
    "depois", [("2026/08", "2026/09"), ("2026/08", "2026/09", "2026/10", "2026/11")]
)
def test_suspeitas_com_o_filtrado_diferente_do_retrato_seguem_dado_suspeito(
    sessao, politica, depois
):
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn(_em_ordem(VELHAS, _abertas(*[(mes, 3340) for mes in depois])))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == MOTIVO_DADO_SUSPEITO
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.mensalidades_abertas == 3
    assert cobranca.divida_centavos == 10020


def test_suspeitas_sem_ult_mesref_seguem_dado_suspeito(sessao, politica):
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=None)
    ctn = _ctn(_em_ordem(VELHAS, NOVAS))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == MOTIVO_DADO_SUSPEITO
    assert cobranca.conferida_em is None


def test_o_desempate_respeita_o_mes_exato_do_ult_mesref(sessao, politica):
    # O mês do `ult_mesref` (2026/07) fica fora e o seguinte (2026/08) entra: com 2026/07 dentro
    # seriam 4 contra um retrato de 3, e com 2026/08 fora, 2.
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn(_em_ordem(VELHAS, NOVAS))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert "2026/07" not in conferencia.meses_em_aberto
    assert "2026/08" in conferencia.meses_em_aberto


def test_o_desempate_so_vale_se_o_filtrado_for_crivel(sessao, politica):
    # Os 3 depois do `ult_mesref` batem com o retrato, mas um deles tem valor zero.
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    novas = _abertas(("2026/08", 3340), ("2026/09", 0), ("2026/10", 3340))
    ctn = _ctn(_em_ordem(VELHAS, novas))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == MOTIVO_DADO_SUSPEITO


def test_antes_do_envio_com_o_desempate_confere(sessao, politica):
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn(_em_ordem(VELHAS, NOVAS))

    resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA)

    assert resultado is ResultadoConferencia.CONFERIDA
    assert cobranca.mensalidades_abertas == 3


def test_sem_abertas_o_ult_mesref_nao_entra_e_quita_como_antes(sessao, politica):
    # 3 rejeitadas pagas cobrem o retrato de 3: quita, com ou sem `ult_mesref`.
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ULT)
    ctn = _ctn([], rejeitadas=3)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is True
    assert cobranca.meses_em_aberto == []


# ---- na mesma rodada a reconferência só encolhe a dívida (D55) ----

ANTIGAS = _abertas(("2025/03", 3340), ("2025/09", 3340), ("2026/05", 3340))  # até o `ult_mesref`
RECENTES = ("2026/08", "2026/09", "2026/10")


def _conferida_hoje(sessao, *, ult_mesref=ULT, conferida_em=AGORA):
    """O retrato de 3 meses, já conferida hoje com os 3 meses posteriores ao `ult_mesref`."""
    cobranca = _cobranca(sessao, meses=3, divida=10020, ult_mesref=ult_mesref)
    cobranca.mensalidades_retrato = 3
    cobranca.conferida_em = conferida_em
    cobranca.meses_em_aberto = list(RECENTES)
    sessao.commit()
    return cobranca


def _recentes(*meses):
    return _abertas(*[(mes, 3340) for mes in meses])


def test_paga_um_recente_os_antigos_nao_entram_e_a_divida_encolhe(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/08", "2026/09")))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia == Conferencia(
        meses_em_aberto=["2026/08", "2026/09"], divida_centavos=6680, propenso=False, conferida=True
    )
    assert cobranca.meses_em_aberto == ["2026/08", "2026/09"]
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.divida_centavos == 6680


def test_paga_dois_recentes_cobra_so_o_que_sobrou_dos_conhecidos(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/09")))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert conferencia.meses_em_aberto == ["2026/09"]
    assert conferencia.divida_centavos == 3340
    assert cobranca.mensalidades_abertas == 1
    assert cobranca.divida_centavos == 3340


def test_paga_os_tres_recentes_quita_e_os_antigos_nao_seguram(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    ctn = _ctn(ANTIGAS, rejeitadas=6)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is True
    assert cobranca.meses_em_aberto == []
    assert cobranca.conferida_em == AGORA


def test_mes_novo_no_meio_do_dia_nao_entra_ate_a_proxima_importacao(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/09", "2026/10", "2026/11")))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.meses_em_aberto == ["2026/09", "2026/10"]


def test_a_reconferencia_sem_pagamento_nao_muda_nada(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes(*RECENTES)))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.meses_em_aberto == list(RECENTES)
    assert conferencia.divida_centavos == 10020
    assert cobranca.mensalidades_abertas == 3


def test_conferida_hoje_funciona_sem_ult_mesref_tambem(sessao, politica):
    # A reimportação por CSV zera o `ult_mesref`; a regra do dia não depende dele.
    cobranca = _conferida_hoje(sessao, ult_mesref=None)
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/08", "2026/09")))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.meses_em_aberto == ["2026/08", "2026/09"]


def test_conferida_ontem_nao_restringe_aos_meses_gravados(sessao, politica):
    # Em outro dia vale a regra de sempre: as abertas suspeitas sem desempate são `dado_suspeito`.
    cobranca = _conferida_hoje(sessao, conferida_em=AGORA - timedelta(days=1))
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/08", "2026/09")))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == MOTIVO_DADO_SUSPEITO


def test_conferida_hoje_com_a_lista_vazia_quita_sem_mes_novo(sessao, politica):
    cobranca = _conferida_hoje(sessao)
    cobranca.meses_em_aberto = []
    sessao.commit()
    ctn = _ctn(_em_ordem(ANTIGAS, _recentes("2026/10")), rejeitadas=6)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is True


def test_o_patamar_do_suspeito_e_o_retrato_e_nao_as_mensalidades_gravadas(sessao, politica):
    # Retrato de 3; a conferência anterior já gravou `mensalidades_abertas = 4` (limite: 3 + 1).
    # Com 5 abertas o teste deve ser contra o retrato (3 + 1 = 4) e não contra o 4 gravado.
    cobranca = _cobranca(sessao, meses=4, divida=13360, ult_mesref=None)
    cobranca.mensalidades_retrato = 3
    sessao.commit()
    ctn = _ctn(_recentes("2026/06", "2026/07", "2026/08", "2026/09", "2026/10"))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is False
    assert conferencia.motivo == MOTIVO_DADO_SUSPEITO


def test_quatro_abertas_com_retrato_de_tres_ainda_conferem_e_nao_derivam(sessao, politica):
    cobranca = _cobranca(sessao, meses=3, divida=10020)
    cobranca.mensalidades_retrato = 3
    sessao.commit()
    ctn = _ctn(_recentes("2026/07", "2026/08", "2026/09", "2026/10"))

    primeira = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)
    assert primeira.conferida is True
    assert cobranca.mensalidades_abertas == 4

    # Uma 5ª aberta aparece: contra o retrato (3 + 1) é suspeita, mesmo com 4 gravadas.
    ctn = _ctn(_recentes("2026/06", "2026/07", "2026/08", "2026/09", "2026/10"))
    cobranca.conferida_em = None
    segunda = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)
    assert segunda.conferida is False
    assert segunda.motivo == MOTIVO_DADO_SUSPEITO
