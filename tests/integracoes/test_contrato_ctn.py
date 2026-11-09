"""Contrato do CTN. Toda implementação (a falsa, o `CTNEficaz` e o composto) passa por estes testes.

Há dois contratos. O `ContratoLeituraCTN` vale para quem só lê (o `CTNEficaz`); o `ContratoCTN`
acrescenta a escrita (link, cadastro do cartão e baixa) e vale para o `Protocol CTN` inteiro.

Para testar outra implementação, herde de um deles e defina as fixtures `ctn_e_matricula`, que
devolve a implementação e uma matrícula existente com histórico de pagamentos, mensalidades em
aberto e presença no retrato e nas baixas de `DATA_REF`, e `ctn_fora_do_ar_e_matricula`.
"""

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from agente.integracoes.ctn import (
    BaixaCTN,
    ClienteCTN,
    ClienteNaoEncontrado,
    ConsultaMensalidades,
    CTNComposto,
    CTNFalso,
    CTNIndisponivel,
    InadimplenteCTN,
    MensalidadeAberta,
)
from agente.tipos import FormaHistorico, PagamentoHistorico

PAGO_EM = datetime(2026, 10, 22, 15, tzinfo=UTC)
DATA_REF = date(2026, 11, 6)
# As baixas do contrato foram coletadas depois deste instante.
COLETADAS_DEPOIS_DE = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)


class ContratoLeituraCTN:
    def test_consulta_o_cliente(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        cliente = ctn.consultar_cliente(matricula)
        assert cliente.matricula == matricula
        assert cliente.nome
        assert cliente.telefone.startswith("+55")

    def test_cliente_inexistente_levanta_erro(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        with pytest.raises(ClienteNaoEncontrado):
            ctn.consultar_cliente("NAO-EXISTE")

    def test_historico_vem_em_ordem_e_tipado(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        historico = ctn.historico_pagamentos(matricula, meses=12)
        assert historico
        assert all(isinstance(p, PagamentoHistorico) for p in historico)
        assert [p.data for p in historico] == sorted(p.data for p in historico)

    def test_o_retrato_do_dia_vem_tipado(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        retrato = list(ctn.inadimplentes(DATA_REF))
        assert matricula in {i.matricula for i in retrato}
        for item in retrato:
            assert isinstance(item, InadimplenteCTN)
            assert item.data_ref == DATA_REF
            assert item.nome
            assert isinstance(item.meses_atraso, int)
            assert isinstance(item.meses_ate_corte, int)
            assert item.ult_mesref is None or isinstance(item.ult_mesref, date)

    def test_o_retrato_de_um_dia_sem_dados_vem_vazio(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        assert list(ctn.inadimplentes(date(2020, 1, 1))) == []

    def test_mensalidades_em_aberto_vem_tipada(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        abertas = ctn.mensalidades_em_aberto(matricula)
        assert abertas
        for aberta in abertas:
            assert isinstance(aberta, MensalidadeAberta)
            assert re.fullmatch(r"\d{4}/\d{2}", aberta.mes_ano)
            assert aberta.valor_centavos > 0

    def test_consultar_mensalidades_traz_as_abertas_e_a_contagem_das_rejeitadas(
        self, ctn_e_matricula
    ):
        ctn, matricula = ctn_e_matricula
        consulta = ctn.consultar_mensalidades(matricula)
        assert isinstance(consulta, ConsultaMensalidades)
        assert consulta.abertas == ctn.mensalidades_em_aberto(matricula)
        assert consulta.rejeitadas_lidas >= len(consulta.abertas) > 0

    def test_baixas_desde_o_cursor_vem_em_ordem_e_tipadas(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        baixas = ctn.baixas_desde(COLETADAS_DEPOIS_DE)
        assert baixas
        for baixa in baixas:
            assert isinstance(baixa, BaixaCTN)
            assert isinstance(baixa.forma, FormaHistorico)
            assert baixa.id_externo
            assert baixa.valor_centavos > 0
            assert baixa.coletado_em >= COLETADAS_DEPOIS_DE
        assert [b.coletado_em for b in baixas] == sorted(b.coletado_em for b in baixas)

    def test_o_cursor_de_baixas_e_inclusivo(self, ctn_e_matricula):
        # Uma baixa coletada no instante do cursor volta (nao se perde a de mesma coleta); so o
        # consumidor, idempotente pelo `id_externo`, descarta a que ja viu.
        ctn, _ = ctn_e_matricula
        ultimo = ctn.baixas_desde(COLETADAS_DEPOIS_DE)[-1].coletado_em
        no_cursor = ctn.baixas_desde(ultimo)
        assert no_cursor
        assert all(baixa.coletado_em == ultimo for baixa in no_cursor)
        assert len({baixa.id_externo for baixa in no_cursor}) == len(no_cursor)

    def test_baixas_desde_depois_da_ultima_coleta_vem_vazia(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        ultimo = ctn.baixas_desde(COLETADAS_DEPOIS_DE)[-1].coletado_em
        assert ctn.baixas_desde(ultimo + timedelta(seconds=1)) == []

    def test_matricula_desconhecida_vem_vazia_no_historico_e_nas_mensalidades(
        self, ctn_e_matricula
    ):
        # O CTN real nao distingue "sem baixas" de "matricula inexistente": o contrato e a lista
        # vazia. So o `consultar_cliente` levanta ClienteNaoEncontrado.
        ctn, _ = ctn_e_matricula
        assert ctn.historico_pagamentos("NAO-EXISTE", meses=12) == []
        assert ctn.mensalidades_em_aberto("NAO-EXISTE") == []
        assert ctn.consultar_mensalidades("NAO-EXISTE") == ConsultaMensalidades([], 0)

    # A implementação real define a fixture `ctn_fora_do_ar_e_matricula`: o CTN sem resposta (rede
    # caída, prazo estourado ou 5xx). A falha tem de sair como CTNIndisponivel, que o turno trata.
    @pytest.mark.parametrize(
        "chamada",
        [
            lambda ctn, m: ctn.consultar_cliente(m),
            lambda ctn, m: ctn.historico_pagamentos(m, meses=12),
            lambda ctn, m: list(ctn.inadimplentes(DATA_REF)),
            lambda ctn, m: ctn.mensalidades_em_aberto(m),
            lambda ctn, m: ctn.consultar_mensalidades(m),
            lambda ctn, m: ctn.baixas_desde(COLETADAS_DEPOIS_DE),
        ],
        ids=["cliente", "historico", "retrato", "mensalidades", "consulta", "baixas_desde"],
    )
    def test_com_o_ctn_fora_do_ar_a_leitura_sai_como_indisponivel(
        self, ctn_fora_do_ar_e_matricula, chamada
    ):
        ctn, matricula = ctn_fora_do_ar_e_matricula
        with pytest.raises(CTNIndisponivel):
            chamada(ctn, matricula)


class ContratoCTN(ContratoLeituraCTN):
    def test_link_de_pagamento_leva_o_valor(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        link = ctn.gerar_link_pagamento(matricula, valor_centavos=7020, cobranca_id=1)
        assert link.url.startswith("https://")
        assert link.id_externo

    def test_link_com_valor_invalido_e_recusado(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        with pytest.raises(ValueError, match="valor"):
            ctn.gerar_link_pagamento(matricula, valor_centavos=0, cobranca_id=1)

    def test_link_de_cadastro_do_cartao(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        assert ctn.gerar_link_cadastro_cartao(matricula).url.startswith("https://")

    def test_baixa_repetida_nao_duplica(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        primeira = ctn.registrar_baixa(matricula, 10020, PAGO_EM, referencia="ref-1")
        repetida = ctn.registrar_baixa(matricula, 10020, PAGO_EM, referencia="ref-1")
        assert primeira is True
        assert repetida is False

    @pytest.mark.parametrize(
        "chamada",
        [
            lambda ctn, m: ctn.gerar_link_pagamento(m, valor_centavos=7020, cobranca_id=1),
            lambda ctn, m: ctn.gerar_link_cadastro_cartao(m),
            lambda ctn, m: ctn.registrar_baixa(m, 10020, PAGO_EM, referencia="ref-1"),
        ],
        ids=["link", "cartao", "baixa"],
    )
    def test_com_o_ctn_fora_do_ar_a_escrita_sai_como_indisponivel(
        self, ctn_fora_do_ar_e_matricula, chamada
    ):
        ctn, matricula = ctn_fora_do_ar_e_matricula
        with pytest.raises(CTNIndisponivel):
            chamada(ctn, matricula)


def _falso_com_dados() -> tuple[CTNFalso, str]:
    cliente = ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", cpf=None)
    historico = [
        PagamentoHistorico(date(2026, 9, 10), FormaHistorico.PIX, 3340),
        PagamentoHistorico(date(2026, 7, 10), FormaHistorico.PIX, 3340),
        PagamentoHistorico(date(2026, 8, 11), FormaHistorico.PIX, 3340),
    ]
    retrato = [
        InadimplenteCTN(
            "M-0001", "Cliente Fictício", "+5583900000001", 2, 4, date(2026, 9, 1), DATA_REF
        ),
        InadimplenteCTN(
            "M-0002", "Outro Fictício", "+5583900000002", 1, 5, date(2026, 10, 1), DATA_REF
        ),
        InadimplenteCTN(
            "M-0001",
            "Cliente Fictício",
            "+5583900000001",
            1,
            5,
            date(2026, 10, 1),
            date(2026, 11, 5),
        ),
    ]
    abertas = {"M-0001": [MensalidadeAberta("2026/09", 3340), MensalidadeAberta("2026/10", 3340)]}
    baixas = [
        BaixaCTN(
            "bx-2",
            "M-0002",
            "2026/10",
            3340,
            FormaHistorico.BOLETO,
            datetime(2026, 11, 6, 11, tzinfo=UTC),
            datetime(2026, 11, 6, 12, tzinfo=UTC),
        ),
        BaixaCTN(
            "bx-1",
            "M-0001",
            "2026/08",
            3340,
            FormaHistorico.PIX,
            datetime(2026, 11, 6, 10, tzinfo=UTC),
            datetime(2026, 11, 6, 10, 5, tzinfo=UTC),
        ),
        BaixaCTN(
            "bx-0",
            "M-0001",
            "2026/07",
            3340,
            FormaHistorico.PIX,
            datetime(2026, 11, 5, 10, tzinfo=UTC),
            datetime(2026, 11, 5, 10, 5, tzinfo=UTC),
        ),
    ]
    ctn = CTNFalso(
        clientes=[cliente],
        historicos={"M-0001": historico},
        hoje=date(2026, 10, 22),
        retrato=retrato,
        mensalidades_abertas=abertas,
        baixas_externas=baixas,
    )
    return ctn, "M-0001"


class TestCTNComposto(ContratoCTN):
    """Leitura de um lado e escrita do outro, e o contrato inteiro continua valendo."""

    @pytest.fixture
    def ctn_e_matricula(self):
        leitura, matricula = _falso_com_dados()
        escrita = CTNFalso(clientes=[leitura.consultar_cliente(matricula)])
        return CTNComposto(leitura, escrita), matricula

    @pytest.fixture
    def ctn_fora_do_ar_e_matricula(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.leitura.fora_do_ar = True
        ctn.escrita.fora_do_ar = True
        return ctn, matricula

    def test_as_leituras_vao_a_leitura_e_as_escritas_a_escrita(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.consultar_cliente(matricula)
        ctn.historico_pagamentos(matricula, meses=12)
        list(ctn.inadimplentes(DATA_REF))
        ctn.mensalidades_em_aberto(matricula)
        ctn.consultar_mensalidades(matricula)
        ctn.baixas_desde(COLETADAS_DEPOIS_DE)
        link = ctn.gerar_link_pagamento(matricula, valor_centavos=7020, cobranca_id=3)
        cartao = ctn.gerar_link_cadastro_cartao(matricula)
        ctn.registrar_baixa(matricula, 7020, PAGO_EM, referencia="ref-9")

        assert ctn.leitura.links == {} and ctn.leitura.baixas == []
        assert set(ctn.escrita.links) == {link.id_externo, cartao.id_externo}
        assert ctn.escrita.baixas == [(matricula, 7020, PAGO_EM, "ref-9")]

    def test_fechar_fecha_a_leitura_que_tem_cliente_http(self, ctn_e_matricula):
        class Leitura:
            fechada = False

            def fechar(self):
                self.fechada = True

        leitura = Leitura()
        CTNComposto(leitura, ctn_e_matricula[0].escrita).fechar()
        assert leitura.fechada

    def test_fechar_com_a_leitura_falsa_nao_faz_nada(self, ctn_e_matricula):
        ctn_e_matricula[0].fechar()


class TestCTNFalso(ContratoCTN):
    @pytest.fixture
    def ctn_fora_do_ar_e_matricula(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.fora_do_ar = True
        return ctn, matricula

    @pytest.fixture
    def ctn_e_matricula(self):
        return _falso_com_dados()

    def test_falsa_grava_as_baixas(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.registrar_baixa(matricula, 10020, PAGO_EM, referencia="ref-1")
        assert ctn.baixas == [(matricula, 10020, PAGO_EM, "ref-1")]

    def test_falsa_lembra_o_valor_de_cada_link(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        link = ctn.gerar_link_pagamento(matricula, valor_centavos=4020, cobranca_id=7)
        assert ctn.links[link.id_externo] == (matricula, 4020, 7)

    def test_falsa_filtra_o_historico_pelos_meses(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        datas = [p.data for p in ctn.historico_pagamentos(matricula, meses=3)]
        assert datas == [date(2026, 8, 11), date(2026, 9, 10)]

    def test_falsa_filtra_o_retrato_pela_data(self, ctn_e_matricula):
        ctn, _ = ctn_e_matricula
        ontem = list(ctn.inadimplentes(date(2026, 11, 5)))
        assert [(i.matricula, i.meses_atraso) for i in ontem] == [("M-0001", 1)]

    def test_falsa_sem_dados_novos_responde_vazio(self):
        ctn = CTNFalso(aceita_qualquer_matricula=True)
        assert list(ctn.inadimplentes(DATA_REF)) == []
        assert ctn.mensalidades_em_aberto("DEMO-0001") == []
        assert ctn.consultar_mensalidades("DEMO-0001") == ConsultaMensalidades([], 0)
        assert ctn.baixas_desde(COLETADAS_DEPOIS_DE) == []


def test_a_falsa_diz_quantas_rejeitadas_leu_quando_o_teste_configura():
    ctn = CTNFalso(
        mensalidades_abertas={"M-1": [MensalidadeAberta("2026/10", 3340)]},
        rejeitadas_lidas={"M-1": 3, "M-2": 2},
    )
    assert ctn.consultar_mensalidades("M-1").rejeitadas_lidas == 3
    # Todas pagas: havia rejeitadas e nenhuma aberta.
    assert ctn.consultar_mensalidades("M-2") == ConsultaMensalidades([], 2)


def test_a_falsa_aberta_aceita_qualquer_matricula_e_gera_o_link():
    # A demo do WhatsApp (modo ativo, piloto) usa o CTN falso sem a carteira carregada nele.
    ctn = CTNFalso(aceita_qualquer_matricula=True)
    cliente = ctn.consultar_cliente("DEMO-0001")
    assert cliente.matricula == "DEMO-0001"
    assert cliente.cpf is None
    link = ctn.gerar_link_pagamento("DEMO-0001", valor_centavos=4020, cobranca_id=1)
    assert link.url.startswith("https://ctn.invalid/")


def test_a_falsa_comum_continua_recusando_a_matricula_desconhecida():
    with pytest.raises(ClienteNaoEncontrado):
        CTNFalso().consultar_cliente("DEMO-0001")


def test_ctn_falso_novo_nao_repete_o_id_do_link_de_outro_processo():
    """A11: o contador volta a 1 a cada processo; o banco da demo guarda os links antigos."""
    um = CTNFalso(aceita_qualquer_matricula=True)
    outro = CTNFalso(aceita_qualquer_matricula=True)
    ids_um = {um.gerar_link_pagamento("M1", 7020, 1).id_externo for _ in range(3)}
    ids_outro = {outro.gerar_link_pagamento("M1", 7020, 1).id_externo for _ in range(3)}
    assert len(ids_um) == 3
    assert ids_um.isdisjoint(ids_outro)
