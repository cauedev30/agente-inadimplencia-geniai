"""Contrato do CTN. Toda implementação (a falsa agora, a real na tarefa 6.5) passa por estes testes.

Para testar outra implementação, herde de ContratoCTN e defina a fixture `ctn_e_matricula`, que
devolve a implementação e uma matrícula existente com histórico de pagamentos.
"""

from datetime import UTC, date, datetime

import pytest

from agente.integracoes.ctn import ClienteCTN, ClienteNaoEncontrado, CTNFalso
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


class TestCTNFalso(ContratoCTN):
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
