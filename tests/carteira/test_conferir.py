"""A conferência no contato (spec do CTN real, seção 4): os meses e o valor exatos de um cliente."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from agente.carteira.conferir import (
    Conferencia,
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


def _cobranca(sessao, meses=3, divida=10020, regua=Regua.R3, matricula=MATRICULA):
    cliente = Cliente(matricula_ctn=matricula, nome="Cliente Fictício", telefone="+5583900000001")
    cobranca = Cobranca(
        cliente=cliente,
        regua=regua,
        mensalidades_abertas=meses,
        divida_centavos=divida,
        propenso_cartao=False,
        campanha="nov",
    )
    sessao.add(cobranca)
    sessao.commit()
    return cobranca


def _abertas(*itens):
    return [MensalidadeAberta(mes, valor) for mes, valor in itens]


def _ctn(abertas, historico=None, matricula=MATRICULA, cls=CTNEspiao):
    return cls(
        mensalidades_abertas={matricula: abertas},
        historicos={matricula: historico or []},
        hoje=date(2026, 11, 9),
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


def test_zero_meses_em_aberto_nao_encerra_e_nao_mexe_na_divida(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn([])

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida is True
    assert conferencia.meses_em_aberto == []
    assert conferencia.quitada_no_ctn is True
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.encerrada_motivo is None
    assert cobranca.meses_em_aberto == []
    assert cobranca.conferida_em == AGORA
    # Os valores do retrato ficam: a decisão é de quem chamou.
    assert cobranca.divida_centavos == 10020
    assert cobranca.mensalidades_abertas == 3
    # O histórico não é lido quando não há dívida a cobrar.
    assert ctn.chamadas == ["mensalidades"]


def test_matricula_desconhecida_e_tratada_como_zero_meses(sessao, politica):
    cobranca = _cobranca(sessao, matricula="M-9999")
    ctn = CTNEspiao(hoje=date(2026, 11, 9))

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.quitada_no_ctn is True
    assert cobranca.status is StatusCobranca.ATIVA


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

    assert conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA) is True

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


def test_antes_do_envio_bloqueia_a_quitada_no_ctn_no_mesmo_dia(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn([])

    assert conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA) is False
    # Mesmo dia: não chama o CTN de novo e continua bloqueada.
    assert (
        conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA + timedelta(hours=1))
        is False
    )
    assert ctn.chamadas == ["mensalidades"]


def test_quitada_de_ontem_nao_bloqueia_se_o_ctn_cai_hoje(sessao, politica):
    cobranca = _cobranca(sessao)
    cobranca.conferida_em = AGORA - timedelta(days=1)
    cobranca.meses_em_aberto = []
    sessao.commit()
    ctn = _ctn([])
    ctn.fora_do_ar = True

    assert conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA) is True


def test_antes_do_envio_com_ctn_fora_libera_com_o_retrato(sessao, politica):
    cobranca = _cobranca(sessao)
    ctn = _ctn(_abertas(("2026/10", 3340)))
    ctn.fora_do_ar = True

    assert conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, AGORA) is True
    assert cobranca.conferida_em is None
    assert cobranca.divida_centavos == 10020
