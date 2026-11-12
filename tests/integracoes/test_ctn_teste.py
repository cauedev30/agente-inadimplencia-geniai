"""O CTN do modo teste (D56): os casos do cardápio, o roteador e os textos [TESTE]."""

import tomllib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agente.carteira.conferir import conferir_cobranca
from agente.db.modelos import Cliente, Cobranca
from agente.dominio.divida import classificar_regua
from agente.dominio.propensao import eh_propenso
from agente.integracoes.ctn import (
    BaixaCTN,
    ClienteCTN,
    ClienteNaoEncontrado,
    ConsultaMensalidades,
    CTNFalso,
    MensalidadeAberta,
)
from agente.integracoes.ctn_teste import (
    PREFIXO_SIMULADO,
    CasoInexistente,
    CTNDeCasos,
    CTNEscritaDeTeste,
    RoteadorCTN,
    matricula_simulada,
    numero_do_caso,
)
from agente.integracoes.pix import PixDeTeste, StatusPix, TxidRepetido
from agente.politica import CasoTeste
from agente.tipos import FormaHistorico, StatusCobranca
from agente.turno.acoes import Acao
from agente.turno.executar import Numeros
from agente.turno.redacao import interpolar
from tests.conftest import RAIZ
from tests.integracoes.test_contrato_ctn import DATA_REF, ContratoLeituraCTN

HOJE = date(2026, 11, 10)
AGORA = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("America/Fortaleza"))
MENSALIDADE = 3340
TELEFONE = "+5583900000001"
SIM_1 = matricula_simulada(1, TELEFONE)
SIM_2 = matricula_simulada(2, TELEFONE)
SIM_3 = matricula_simulada(3, TELEFONE)
SIM_4 = matricula_simulada(4, TELEFONE)
SIM_5 = matricula_simulada(5, TELEFONE)


def _casos() -> list[CasoTeste]:
    cardapio = tomllib.loads((RAIZ / "cardapio-teste.toml").read_text(encoding="utf-8"))
    return [CasoTeste.model_validate(caso) for caso in cardapio["caso"]]


def _ctn_de_casos(hoje: date = HOJE) -> CTNDeCasos:
    return CTNDeCasos(_casos(), MENSALIDADE, lambda: hoje)


def _meses(ctn: CTNDeCasos, matricula: str) -> list[str]:
    return [aberta.mes_ano for aberta in ctn.mensalidades_em_aberto(matricula)]


# ---- a matrícula simulada ----


def test_a_matricula_simulada_leva_o_caso_e_os_4_ultimos_digitos_do_telefone():
    assert PREFIXO_SIMULADO == "SIM-"
    assert matricula_simulada(3, "+5583987651234") == "SIM-3-1234"


@pytest.mark.parametrize(
    ("matricula", "numero"),
    [
        ("SIM-1-0001", 1),
        ("SIM-12-9999", 12),
        ("SIM-1", None),
        ("SIM-a-0001", None),
        ("SIM-1-001", None),
        ("M-0001", None),
        ("sim-1-0001", None),
        ("xSIM-1-0001", None),
    ],
)
def test_o_numero_do_caso_sai_da_matricula(matricula, numero):
    assert numero_do_caso(matricula) == numero


# ---- o contrato das leituras ----


class TestCTNDeCasos(ContratoLeituraCTN):
    """Passa pelo contrato de leitura. O retrato e as baixas vêm vazios por desenho (a carteira e
    as baixas são do CTN real), e a memória não cai: esses testes do contrato são trocados."""

    @pytest.fixture
    def ctn_e_matricula(self):
        ctn = _ctn_de_casos()
        ctn.ativar(SIM_1, TELEFONE)
        return ctn, SIM_1

    def test_o_retrato_do_dia_vem_tipado(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        assert list(ctn.inadimplentes(DATA_REF)) == []

    def test_baixas_desde_o_cursor_vem_em_ordem_e_tipadas(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        assert ctn.baixas_desde(datetime(2026, 11, 6, tzinfo=UTC)) == []

    def test_o_cursor_de_baixas_e_inclusivo(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        assert ctn.baixas_desde(datetime(2026, 11, 6, tzinfo=UTC), {"x"}) == []

    def test_os_vistos_do_cursor_nao_voltam(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.pagar_meses(matricula, 1, AGORA, "m1")
        assert ctn.baixas_desde(datetime(2026, 1, 1, tzinfo=UTC), {"SIM-m1-1"}) == []

    def test_baixas_desde_depois_da_ultima_coleta_vem_vazia(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        assert ctn.baixas_desde(datetime(2030, 1, 1, tzinfo=UTC)) == []

    def test_com_o_ctn_fora_do_ar_a_leitura_sai_como_indisponivel(self, ctn_e_matricula):
        # Não há rede, então nada levanta CTNIndisponivel: as leituras só respondem.
        ctn, matricula = ctn_e_matricula
        assert ctn.consultar_cliente(matricula)
        assert ctn.historico_pagamentos(matricula, meses=12)
        assert ctn.consultar_mensalidades(matricula).abertas


# ---- as leituras ----


def test_o_cliente_e_o_do_caso_com_o_telefone_do_ativar():
    ctn = _ctn_de_casos()
    ctn.ativar(SIM_2, TELEFONE)
    assert ctn.consultar_cliente(SIM_2) == ClienteCTN(SIM_2, "Bruno", TELEFONE, "20234567800")


def test_o_cliente_sem_o_ativar_vem_sem_telefone():
    assert _ctn_de_casos().consultar_cliente(SIM_1).telefone == ""


@pytest.mark.parametrize("matricula", ["M-0001", "SIM-9-0001", "SIM-1", "xx000000001"])
def test_matricula_que_nao_e_de_um_caso_do_cardapio_nao_e_encontrada(matricula):
    ctn = _ctn_de_casos()
    with pytest.raises(ClienteNaoEncontrado):
        ctn.consultar_cliente(matricula)
    assert ctn.mensalidades_em_aberto(matricula) == []
    assert ctn.consultar_mensalidades(matricula) == ConsultaMensalidades([], 0)
    assert ctn.historico_pagamentos(matricula, meses=12) == []


def test_as_mensalidades_em_aberto_sao_os_meses_antes_do_mes_de_hoje():
    ctn = _ctn_de_casos()
    assert ctn.mensalidades_em_aberto(SIM_1) == [
        MensalidadeAberta("2026/08", MENSALIDADE),
        MensalidadeAberta("2026/09", MENSALIDADE),
        MensalidadeAberta("2026/10", MENSALIDADE),
    ]
    assert _meses(ctn, SIM_5) == [
        "2026/05",
        "2026/06",
        "2026/07",
        "2026/08",
        "2026/09",
        "2026/10",
    ]


def test_a_virada_do_ano_conta_os_meses_certos():
    ctn = _ctn_de_casos(date(2027, 2, 3))
    assert _meses(ctn, SIM_1) == ["2026/11", "2026/12", "2027/01"]


def test_consultar_mensalidades_traz_as_rejeitadas_lidas_do_caso():
    ctn = _ctn_de_casos()
    consulta = ctn.consultar_mensalidades(SIM_3)
    assert consulta.rejeitadas_lidas == 4
    assert consulta.abertas == ctn.mensalidades_em_aberto(SIM_3)


def test_o_historico_do_caso_vem_em_datas_reais_antes_do_1o_mes_em_aberto():
    historico = _ctn_de_casos().historico_pagamentos(SIM_1, meses=12)
    assert [(p.data, p.forma, p.valor_centavos) for p in historico] == [
        (date(2026, 5, 10), FormaHistorico.CARTAO, MENSALIDADE),
        (date(2026, 6, 10), FormaHistorico.CARTAO, MENSALIDADE),
        (date(2026, 7, 10), FormaHistorico.CARTAO, MENSALIDADE),
    ]


def test_o_historico_do_caso_misto_traz_as_3_formas():
    historico = _ctn_de_casos().historico_pagamentos(SIM_4, meses=12)
    # O 1º mês em aberto do caso 4 é 2026/06: o histórico vai de março a maio.
    assert [(p.data, p.forma) for p in historico] == [
        (date(2026, 3, 12), FormaHistorico.PIX),
        (date(2026, 4, 20), FormaHistorico.BOLETO),
        (date(2026, 5, 8), FormaHistorico.CARTAO),
    ]


def test_o_caso_sem_historico_vem_vazio():
    assert _ctn_de_casos().historico_pagamentos(SIM_5, meses=12) == []


def test_a_janela_do_historico_conta_a_partir_de_hoje_como_o_ctn_real():
    # O histórico do caso 2 é de maio, junho e julho (dia 5); hoje é 10/11. Como no CTN real, os
    # `meses` contam de hoje (a conferência pede os meses até o `ult_mesref`, D58).
    ctn = _ctn_de_casos()
    assert ctn.historico_pagamentos(SIM_2, meses=4) == []
    assert [p.data for p in ctn.historico_pagamentos(SIM_2, meses=5)] == [date(2026, 7, 5)]
    assert [p.data.month for p in ctn.historico_pagamentos(SIM_2, meses=6)] == [6, 7]
    assert [p.data.month for p in ctn.historico_pagamentos(SIM_2, meses=7)] == [5, 6, 7]


def test_o_mes_base_do_caso_fica_fixo_depois_do_ativar():
    # Meses relativos a `hoje()` deslizariam na virada do mês, no meio da conversa do testador.
    dia = [date(2026, 11, 30)]
    ctn = CTNDeCasos(_casos(), MENSALIDADE, lambda: dia[0])
    ctn.ativar(SIM_1, TELEFONE)
    antes = _meses(ctn, SIM_1)
    ult_mesref = ctn.ult_mesref(SIM_1)
    assert antes == ["2026/08", "2026/09", "2026/10"]

    dia[0] = date(2026, 12, 1)

    assert _meses(ctn, SIM_1) == antes
    assert ctn.ult_mesref(SIM_1) == ult_mesref
    assert len(ctn.pagar_meses(SIM_1, 1, AGORA, "k")) == 1
    assert _meses(ctn, SIM_1) == antes[1:]
    # Ativar de novo recomeça no mês de hoje.
    ctn.ativar(SIM_1, TELEFONE)
    assert _meses(ctn, SIM_1) == ["2026/09", "2026/10", "2026/11"]


def test_o_ativar_devolve_o_estado_anterior_e_o_restaurar_o_traz_de_volta():
    ctn = _ctn_de_casos()
    ctn.ativar(SIM_1, TELEFONE)
    ctn.pagar_meses(SIM_1, 2, AGORA, "k")
    pagos = _meses(ctn, SIM_1)

    anterior = ctn.ativar(SIM_1, TELEFONE)
    assert len(_meses(ctn, SIM_1)) == 3

    ctn.restaurar(SIM_1, anterior)
    assert _meses(ctn, SIM_1) == pagos
    # As baixas do `/paguei` voltam também: o mesmo comando não paga duas vezes.
    assert len(ctn.pagar_meses(SIM_1, 2, AGORA, "k")) == 2
    assert _meses(ctn, SIM_1) == pagos


def test_restaurar_o_estado_de_uma_matricula_que_nao_tinha_nenhum_a_deixa_sem_estado():
    ctn = _ctn_de_casos()
    anterior = ctn.ativar(SIM_3, TELEFONE)
    assert anterior is None
    ctn.pagar_meses(SIM_3, 1, AGORA, "k")
    ctn.restaurar(SIM_3, anterior)
    assert len(_meses(ctn, SIM_3)) == 4


def test_o_ult_mesref_e_o_dia_1_do_mes_anterior_ao_1o_mes_em_aberto():
    ctn = _ctn_de_casos()
    assert ctn.ult_mesref(SIM_1) == date(2026, 7, 1)
    assert ctn.ult_mesref(SIM_5) == date(2026, 4, 1)
    assert _ctn_de_casos(date(2027, 2, 3)).ult_mesref(SIM_1) == date(2026, 10, 1)


def test_o_ult_mesref_de_matricula_fora_do_cardapio_levanta_caso_inexistente():
    with pytest.raises(CasoInexistente):
        _ctn_de_casos().ult_mesref("SIM-9-0001")
    assert issubclass(CasoInexistente, LookupError)


def test_o_retrato_e_as_baixas_vem_vazios_e_o_cache_nao_faz_nada():
    ctn = _ctn_de_casos()
    assert list(ctn.inadimplentes(HOJE)) == []
    assert ctn.baixas_desde(datetime(2026, 1, 1, tzinfo=UTC)) == []
    assert ctn.invalidar_cache(SIM_1) is None


# ---- ativar e pagar_meses ----


def test_ativar_um_caso_fora_do_cardapio_levanta_caso_inexistente():
    with pytest.raises(CasoInexistente):
        _ctn_de_casos().ativar("SIM-9-0001", TELEFONE)


def test_pagar_meses_parcial_paga_os_mais_antigos_em_boleto():
    ctn = _ctn_de_casos()
    pago_em = datetime(2026, 11, 10, 13, tzinfo=UTC)
    baixas = ctn.pagar_meses(SIM_3, 2, pago_em, chave="msg-7")

    assert baixas == [
        BaixaCTN(
            "SIM-msg-7-1", SIM_3, "2026/07", MENSALIDADE, FormaHistorico.BOLETO, pago_em, pago_em
        ),
        BaixaCTN(
            "SIM-msg-7-2", SIM_3, "2026/08", MENSALIDADE, FormaHistorico.BOLETO, pago_em, pago_em
        ),
    ]
    assert _meses(ctn, SIM_3) == ["2026/09", "2026/10"]
    # O CTN real também seguiria dizendo quantas rejeitadas houve: as 2 pagas casam com as baixas.
    assert ctn.consultar_mensalidades(SIM_3).rejeitadas_lidas == 4


def test_pagar_todos_os_meses_deixa_a_conferencia_ver_quitada():
    ctn = _ctn_de_casos()
    ctn.pagar_meses(SIM_1, 3, AGORA, chave="msg-1")
    assert ctn.consultar_mensalidades(SIM_1) == ConsultaMensalidades([], 3)


def test_pagar_de_novo_com_a_mesma_chave_devolve_as_mesmas_baixas_sem_pagar_outra_vez():
    ctn = _ctn_de_casos()
    primeira = ctn.pagar_meses(SIM_4, 2, AGORA, chave="msg-9")
    repetida = ctn.pagar_meses(SIM_4, 2, datetime(2026, 11, 10, 15, tzinfo=UTC), chave="msg-9")
    assert repetida == primeira
    assert len(_meses(ctn, SIM_4)) == 3
    ctn.pagar_meses(SIM_4, 1, AGORA, chave="msg-10")
    assert len(_meses(ctn, SIM_4)) == 2


@pytest.mark.parametrize("n", [0, -1, 4])
def test_pagar_n_fora_de_1_ate_os_abertos_levanta_value_error(n):
    ctn = _ctn_de_casos()
    with pytest.raises(ValueError):
        ctn.pagar_meses(SIM_1, n, AGORA, chave="x")
    assert len(_meses(ctn, SIM_1)) == 3


def test_pagar_mais_do_que_resta_depois_de_pagar_levanta_value_error():
    ctn = _ctn_de_casos()
    ctn.pagar_meses(SIM_1, 2, AGORA, chave="a")
    with pytest.raises(ValueError):
        ctn.pagar_meses(SIM_1, 2, AGORA, chave="b")
    assert len(_meses(ctn, SIM_1)) == 1


def test_pagar_meses_de_matricula_fora_do_cardapio_levanta_caso_inexistente():
    with pytest.raises(CasoInexistente):
        _ctn_de_casos().pagar_meses("SIM-9-0001", 1, AGORA, chave="x")


def test_ativar_volta_a_matricula_ao_comeco_do_caso():
    ctn = _ctn_de_casos()
    ctn.pagar_meses(SIM_1, 3, AGORA, chave="a")
    ctn.ativar(SIM_1, "+5583900000099")
    assert len(_meses(ctn, SIM_1)) == 3
    assert ctn.consultar_cliente(SIM_1).telefone == "+5583900000099"


def test_cada_matricula_tem_o_seu_estado():
    ctn = _ctn_de_casos()
    outra = matricula_simulada(1, "+5583900000002")
    ctn.pagar_meses(SIM_1, 3, AGORA, chave="a")
    assert len(_meses(ctn, outra)) == 3


# ---- o roteador ----


class _Espiao(CTNFalso):
    def __init__(self, **resto):
        super().__init__(**resto)
        self.chamadas: list[tuple] = []
        self.fechado = False

    def consultar_cliente(self, matricula):
        self.chamadas.append(("cliente", matricula))
        return super().consultar_cliente(matricula)

    def historico_pagamentos(self, matricula, meses):
        self.chamadas.append(("historico", matricula))
        return super().historico_pagamentos(matricula, meses)

    def mensalidades_em_aberto(self, matricula):
        self.chamadas.append(("abertas", matricula))
        return super().mensalidades_em_aberto(matricula)

    def inadimplentes(self, data_ref, invalidas=None):
        self.chamadas.append(("retrato", data_ref))
        return super().inadimplentes(data_ref, invalidas)

    def baixas_desde(self, coletado_desde, vistos=()):
        self.chamadas.append(("baixas", coletado_desde))
        return super().baixas_desde(coletado_desde, vistos)

    def invalidar_cache(self, matricula):
        self.chamadas.append(("cache", matricula))

    def fechar(self):
        self.fechado = True


def _roteador():
    simulados = _Espiao(
        clientes=[ClienteCTN(SIM_1, "Ana", TELEFONE, None)],
        mensalidades_abertas={SIM_1: [MensalidadeAberta("2026/10", MENSALIDADE)]},
    )
    real = _Espiao(
        clientes=[ClienteCTN("M-0001", "Real Fictícia", "+5583900000009", None)],
        mensalidades_abertas={"M-0001": [MensalidadeAberta("2026/10", MENSALIDADE)]},
    )
    return RoteadorCTN(simulados, real), simulados, real


def test_a_matricula_simulada_vai_aos_simulados_e_a_real_vai_ao_real():
    roteador, simulados, real = _roteador()

    roteador.consultar_cliente(SIM_1)
    roteador.historico_pagamentos(SIM_1, meses=4)
    roteador.mensalidades_em_aberto(SIM_1)
    roteador.consultar_mensalidades(SIM_1)
    roteador.invalidar_cache(SIM_1)
    assert real.chamadas == []
    assert [chamada[0] for chamada in simulados.chamadas] == [
        "cliente",
        "historico",
        "abertas",
        "abertas",
        "cache",
    ]

    simulados.chamadas.clear()
    roteador.consultar_cliente("M-0001")
    roteador.historico_pagamentos("M-0001", meses=4)
    roteador.consultar_mensalidades("M-0001")
    roteador.invalidar_cache("M-0001")
    assert simulados.chamadas == []
    assert real.chamadas[0] == ("cliente", "M-0001")
    assert [chamada[0] for chamada in real.chamadas] == [
        "cliente",
        "historico",
        "abertas",
        "cache",
    ]


def test_a_matricula_simulada_de_caso_inexistente_nao_cai_no_real():
    roteador = RoteadorCTN(_ctn_de_casos(), _Espiao())
    with pytest.raises(ClienteNaoEncontrado):
        roteador.consultar_cliente("SIM-9-0001")
    assert roteador.real.chamadas == []


def test_o_retrato_e_as_baixas_vao_so_ao_real():
    roteador, simulados, real = _roteador()
    list(roteador.inadimplentes(HOJE))
    roteador.baixas_desde(datetime(2026, 11, 10, tzinfo=UTC), {"b-1"})
    assert simulados.chamadas == []
    assert [chamada[0] for chamada in real.chamadas] == ["retrato", "baixas"]


def test_fechar_fecha_o_real_se_ele_tiver_fechar():
    roteador, simulados, real = _roteador()
    roteador.fechar()
    assert real.fechado and not simulados.fechado


def test_fechar_com_o_real_sem_fechar_nao_faz_nada():
    class SemFechar(CTNFalso):
        fechar = None

    RoteadorCTN(_ctn_de_casos(), SemFechar()).fechar()


# ---- as integrações de escrita de teste ----


def test_o_link_de_pagamento_de_teste_leva_o_valor_formatado():
    link = CTNEscritaDeTeste().gerar_link_pagamento(SIM_1, 10020, cobranca_id=1)
    assert link.url == "[TESTE] aqui viria o link de pagamento de R$ 100,20"
    assert link.id_externo.startswith("pagar-")


def test_o_link_de_cadastro_do_cartao_de_teste():
    link = CTNEscritaDeTeste().gerar_link_cadastro_cartao(SIM_1)
    assert link.url == "[TESTE] aqui viria o link para cadastrar o cartão"
    assert link.id_externo.startswith("cartao-")


def test_a_escrita_de_teste_aceita_qualquer_matricula_e_a_baixa_nao_duplica():
    ctn = CTNEscritaDeTeste()
    assert ctn.registrar_baixa("QUALQUER", 10020, AGORA, referencia="r-1") is True
    assert ctn.registrar_baixa("QUALQUER", 10020, AGORA, referencia="r-1") is False
    primeiro = ctn.gerar_link_pagamento("OUTRA", 3340, 2)
    segundo = ctn.gerar_link_pagamento("OUTRA", 3340, 2)
    assert primeiro.id_externo != segundo.id_externo


def test_o_link_de_valor_invalido_continua_recusado():
    with pytest.raises(ValueError, match="valor"):
        CTNEscritaDeTeste().gerar_link_pagamento(SIM_1, 0, cobranca_id=1)


def test_o_pix_de_teste_mantem_o_txid_e_troca_o_br_code():
    pix = PixDeTeste()
    cobranca = pix.criar_cobranca("txid-1", 10020, timedelta(hours=24), "Ana")
    assert cobranca.txid == "txid-1"
    assert cobranca.br_code == ("[TESTE] aqui viria o código Pix de R$ 100,20.")
    assert pix.cobrancas["txid-1"] == cobranca
    assert pix.consultar("txid-1") is StatusPix.PENDENTE
    pix.simular_pagamento("txid-1")
    assert pix.consultar("txid-1") is StatusPix.PAGO


def test_o_pix_de_teste_recusa_valor_invalido_e_txid_repetido():
    pix = PixDeTeste()
    with pytest.raises(ValueError):
        pix.criar_cobranca("t", 0, timedelta(hours=1), "Ana")
    pix.criar_cobranca("t", 100, timedelta(hours=1), "Ana")
    with pytest.raises(TxidRepetido):
        pix.criar_cobranca("t", 100, timedelta(hours=1), "Ana")


def test_a_interpolacao_deixa_o_texto_de_teste_do_pix_e_do_link_sair_inteiro():
    # `interpolar` só recusa o que está FORA dos marcadores: o valor, o "R$" e a "/" do texto de
    # teste, que entram pelo marcador, saem como estão.
    pix = PixDeTeste().criar_cobranca("t", 10020, timedelta(hours=1), "Ana")
    link = CTNEscritaDeTeste().gerar_link_pagamento(SIM_1, 10020, cobranca_id=1)
    numeros = Numeros(
        acoes=(Acao.GENERATE_PIX, Acao.SEND_CTN_LINK),
        divida_centavos=10020,
        mensalidades=3,
        degrau=0,
        desconto_centavos=0,
        valor_final_centavos=10020,
        link_ctn=link.url,
        valor_pix_centavos=10020,
        br_code=pix.br_code,
        pix_vence_em=datetime(2026, 11, 11, 10, tzinfo=UTC),
    )

    texto = interpolar("Pix: {pix_copia_e_cola}. Ou pague aqui: {link_ctn}", numeros)

    assert texto == f"Pix: {pix.br_code}. Ou pague aqui: {link.url}"
    assert "[TESTE] aqui viria o código Pix de R$ 100,20" in texto
    assert "[TESTE] aqui viria o link de pagamento de R$ 100,20" in texto


# ---- o "pronto quando": a conferência com os casos ----


def _cobranca_do_caso(sessao, ctn, politica, numero):
    """A cobrança como a 1.4 a monta: retrato = meses do caso, dívida = meses x mensalidade."""
    caso = _casos()[numero - 1]
    matricula = matricula_simulada(numero, TELEFONE)
    ctn.ativar(matricula, TELEFONE)
    cliente = Cliente(matricula_ctn=matricula, nome=caso.nome, telefone=TELEFONE)
    cobranca = Cobranca(
        cliente=cliente,
        regua=classificar_regua(caso.meses_atraso, None, politica),
        mensalidades_abertas=caso.meses_atraso,
        mensalidades_retrato=caso.meses_atraso,
        divida_centavos=caso.meses_atraso * MENSALIDADE,
        propenso_cartao=False,
        campanha="teste",
        ult_mesref=ctn.ult_mesref(matricula),
    )
    sessao.add(cobranca)
    sessao.commit()
    return cobranca


@pytest.mark.parametrize(
    ("numero", "meses", "divida", "propenso"),
    [
        (1, 3, 10020, False),
        (2, 3, 10020, True),
        (3, 4, 13360, False),
        (4, 5, 16700, False),
        (5, 6, 20040, False),
    ],
)
def test_a_conferencia_com_os_casos_da_os_meses_a_divida_e_a_propensao(
    sessao, politica, numero, meses, divida, propenso
):
    ctn = _ctn_de_casos()
    cobranca = _cobranca_do_caso(sessao, ctn, politica, numero)

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.conferida and conferencia.motivo is None
    assert conferencia.meses_em_aberto == _meses(ctn, matricula_simulada(numero, TELEFONE))
    assert len(conferencia.meses_em_aberto) == meses
    assert conferencia.divida_centavos == divida
    assert conferencia.propenso is propenso
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.propenso_cartao is propenso


def test_so_o_caso_2_sai_propenso_pelo_eh_propenso(politica):
    ctn = _ctn_de_casos()
    propensos = {
        numero
        for numero in range(1, 6)
        if eh_propenso(
            ctn.historico_pagamentos(matricula_simulada(numero, TELEFONE), meses=12), politica
        )
    }
    assert propensos == {2}


def test_o_caso_pago_por_inteiro_confere_como_quitado(sessao, politica):
    ctn = _ctn_de_casos()
    cobranca = _cobranca_do_caso(sessao, ctn, politica, 1)
    ctn.pagar_meses(SIM_1, 3, AGORA, chave="msg-1")

    assert conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA).quitada_no_ctn


def test_o_caso_pago_em_parte_confere_so_os_meses_que_restam(sessao, politica):
    ctn = _ctn_de_casos()
    cobranca = _cobranca_do_caso(sessao, ctn, politica, 3)
    ctn.pagar_meses(SIM_3, 2, AGORA, chave="msg-1")

    conferencia = conferir_cobranca(sessao, ctn, politica, cobranca.id, AGORA)

    assert conferencia.meses_em_aberto == ["2026/09", "2026/10"]
    assert conferencia.divida_centavos == 2 * MENSALIDADE
