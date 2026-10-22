"""Contrato do Pix do banco. A falsa passa agora; a real (tarefa 6.4) herda de ContratoPix."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from agente.integracoes.pix import PixFalso, PixIndisponivel, StatusPix, TxidRepetido, gerar_txid

VALIDADE = timedelta(hours=24)


class ContratoPix:
    def test_cria_cobranca_com_br_code(self, pix):
        txid = gerar_txid(1)
        cobranca = pix.criar_cobranca(txid, 10020, VALIDADE, devedor_nome="Cliente Fictício")
        assert cobranca.txid == txid
        assert cobranca.valor_centavos == 10020
        assert cobranca.br_code
        assert cobranca.expira_em.tzinfo is not None

    def test_cobranca_nova_esta_pendente(self, pix):
        txid = gerar_txid(2)
        pix.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")
        assert pix.consultar(txid) is StatusPix.PENDENTE

    def test_txid_repetido_e_recusado(self, pix):
        txid = gerar_txid(3)
        pix.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")
        with pytest.raises(TxidRepetido):
            pix.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")

    def test_valor_invalido_e_recusado(self, pix):
        with pytest.raises(ValueError, match="valor"):
            pix.criar_cobranca(gerar_txid(4), 0, VALIDADE, devedor_nome="Cliente Fictício")

    # A implementação real define a fixture `pix_fora_do_ar`: o banco sem resposta (rede caída,
    # prazo estourado ou 5xx). A falha tem de sair como PixIndisponivel, que o turno trata.
    def test_criar_cobranca_com_o_banco_fora_do_ar(self, pix_fora_do_ar):
        with pytest.raises(PixIndisponivel):
            pix_fora_do_ar.criar_cobranca(
                gerar_txid(7), 3340, VALIDADE, devedor_nome="Cliente Fictício"
            )

    def test_consultar_com_o_banco_fora_do_ar(self, pix_fora_do_ar):
        with pytest.raises(PixIndisponivel):
            pix_fora_do_ar.consultar(gerar_txid(8))


class TestPixFalso(ContratoPix):
    @pytest.fixture
    def relogio(self):
        return {"agora": datetime(2026, 10, 22, 12, tzinfo=UTC)}

    @pytest.fixture
    def pix(self, relogio):
        return PixFalso(agora=lambda: relogio["agora"])

    @pytest.fixture
    def pix_fora_do_ar(self, pix):
        pix.fora_do_ar = True
        return pix

    def test_volta_a_responder_quando_o_banco_volta(self, pix_fora_do_ar):
        pix_fora_do_ar.fora_do_ar = False
        txid = gerar_txid(9)
        pix_fora_do_ar.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")
        assert pix_fora_do_ar.consultar(txid) is StatusPix.PENDENTE

    def test_simular_pagamento(self, pix):
        txid = gerar_txid(5)
        pix.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")
        pix.simular_pagamento(txid)
        assert pix.consultar(txid) is StatusPix.PAGO

    def test_cobranca_vencida_expira(self, pix, relogio):
        txid = gerar_txid(6)
        pix.criar_cobranca(txid, 3340, VALIDADE, devedor_nome="Cliente Fictício")
        relogio["agora"] += VALIDADE + timedelta(seconds=1)
        assert pix.consultar(txid) is StatusPix.EXPIRADO


def test_txid_segue_o_formato_do_bacen():
    txid = gerar_txid(123)
    assert re.fullmatch(r"[a-zA-Z0-9]{26,35}", txid)


def test_txid_nunca_se_repete_para_a_mesma_cobranca():
    assert gerar_txid(1) != gerar_txid(1)
