"""Contrato do CTN. Toda implementação (a falsa agora, a real na tarefa 6.5) passa por estes testes.

Para testar outra implementação, herde de ContratoCTN e defina a fixture `ctn_e_matricula`, que
devolve a implementação e uma matrícula existente com histórico de pagamentos.
"""

from datetime import UTC, date, datetime

import pytest

from agente.integracoes.ctn import ClienteCTN, ClienteNaoEncontrado, CTNFalso, CTNIndisponivel
from agente.tipos import FormaHistorico, PagamentoHistorico

PAGO_EM = datetime(2026, 10, 22, 15, tzinfo=UTC)


class ContratoCTN:
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

    # A implementação real define a fixture `ctn_fora_do_ar_e_matricula`: o CTN sem resposta (rede
    # caída, prazo estourado ou 5xx). A falha tem de sair como CTNIndisponivel, que o turno trata.
    @pytest.mark.parametrize(
        "chamada",
        [
            lambda ctn, m: ctn.consultar_cliente(m),
            lambda ctn, m: ctn.historico_pagamentos(m, meses=12),
            lambda ctn, m: ctn.gerar_link_pagamento(m, valor_centavos=7020, cobranca_id=1),
            lambda ctn, m: ctn.gerar_link_cadastro_cartao(m),
            lambda ctn, m: ctn.registrar_baixa(m, 10020, PAGO_EM, referencia="ref-1"),
        ],
        ids=["cliente", "historico", "link", "cartao", "baixa"],
    )
    def test_com_o_ctn_fora_do_ar_a_falha_sai_como_indisponivel(
        self, ctn_fora_do_ar_e_matricula, chamada
    ):
        ctn, matricula = ctn_fora_do_ar_e_matricula
        with pytest.raises(CTNIndisponivel):
            chamada(ctn, matricula)


class TestCTNFalso(ContratoCTN):
    @pytest.fixture
    def ctn_fora_do_ar_e_matricula(self, ctn_e_matricula):
        ctn, matricula = ctn_e_matricula
        ctn.fora_do_ar = True
        return ctn, matricula

    @pytest.fixture
    def ctn_e_matricula(self):
        cliente = ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", cpf=None)
        historico = [
            PagamentoHistorico(date(2026, 9, 10), FormaHistorico.PIX, 3340),
            PagamentoHistorico(date(2026, 7, 10), FormaHistorico.PIX, 3340),
            PagamentoHistorico(date(2026, 8, 11), FormaHistorico.PIX, 3340),
        ]
        ctn = CTNFalso(
            clientes=[cliente], historicos={"M-0001": historico}, hoje=date(2026, 10, 22)
        )
        return ctn, "M-0001"

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
