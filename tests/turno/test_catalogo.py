from datetime import UTC, datetime

import pytest

from agente.tipos import Botao, TipoFixa
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, DadosFixa, mensagem_fixa

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)

DADOS = DadosFixa(
    nome="Maria",
    valor_centavos=10020,
    link_cartao="https://ctn.invalid/cartao/1",
    link_canal_externo="https://atendimento.invalid/x",
)


def test_canal_externo_usa_o_texto_da_d19_e_o_botao():
    mensagem = mensagem_fixa(TipoFixa.CANAL_EXTERNO, DADOS)

    assert TEXTO_CANAL_EXTERNO == (
        "Para o problema ser resolvido de forma mais eficaz, entre em contato conosco "
        "por esse canal de atendimento."
    )
    assert mensagem.texto == TEXTO_CANAL_EXTERNO
    assert mensagem.botoes == [
        Botao("url", "Falar com um atendente", "https://atendimento.invalid/x")
    ]


def test_confirmacao_leva_nome_e_valor():
    mensagem = mensagem_fixa(TipoFixa.CONFIRMACAO, DADOS)

    assert "Maria" in mensagem.texto
    assert "R$ 100,20" in mensagem.texto
    assert mensagem.botoes == []


def test_confirmacao_com_convite_e_uma_mensagem_so():
    mensagem = mensagem_fixa(TipoFixa.CONFIRMACAO_CONVITE_CARTAO, DADOS)

    assert "R$ 100,20" in mensagem.texto
    assert "cartão" in mensagem.texto
    assert mensagem.botoes == [Botao("url", "Cadastrar o cartão", "https://ctn.invalid/cartao/1")]


def test_convite_sem_link_do_cartao_e_erro():
    sem_link = DadosFixa("Maria", 10020, None, "https://atendimento.invalid/x")
    with pytest.raises(ValueError, match="link_cartao"):
        mensagem_fixa(TipoFixa.CONFIRMACAO_CONVITE_CARTAO, sem_link)


def test_confirmacao_sem_valor_e_erro():
    sem_valor = DadosFixa("Maria", None, None, "https://atendimento.invalid/x")
    with pytest.raises(ValueError, match="valor"):
        mensagem_fixa(TipoFixa.CONFIRMACAO, sem_valor)


def test_humano_no_whatsapp_usa_o_texto_da_politica_e_nao_tem_botao():
    dados = DadosFixa("Maria", None, None, "", texto="Um atendente segue aqui.")

    mensagem = mensagem_fixa(TipoFixa.HUMANO_NO_WHATSAPP, dados)

    assert mensagem.texto == "Um atendente segue aqui."
    assert mensagem.botoes == []


def test_humano_no_whatsapp_sem_o_texto_e_erro():
    with pytest.raises(ValueError, match="texto"):
        mensagem_fixa(TipoFixa.HUMANO_NO_WHATSAPP, DadosFixa("Maria", None, None, ""))


def test_canal_externo_com_texto_redigido_troca_o_texto_e_mantem_o_botao():
    dados = DadosFixa(
        "Maria", None, None, "https://atendimento.invalid/x", texto="Passei a conversa."
    )

    mensagem = mensagem_fixa(TipoFixa.CANAL_EXTERNO, dados)

    assert mensagem.texto == "Passei a conversa."
    assert mensagem.botoes == [
        Botao("url", "Falar com um atendente", "https://atendimento.invalid/x")
    ]


def test_canal_externo_sem_texto_continua_com_o_da_d19():
    dados = DadosFixa("Maria", None, None, "https://atendimento.invalid/x", texto="")

    assert mensagem_fixa(TipoFixa.CANAL_EXTERNO, dados).texto == TEXTO_CANAL_EXTERNO
