from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agente.tipos import Botao, TipoFixa
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, DadosFixa, mensagem_fixa
from agente.webchat.link import ConfigWebchat, link_canal_externo
from agente.webchat.token import TokenInvalido, ler_token

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


def test_a_6a_mensagem_leva_o_link_com_um_token_valido_para_a_cobranca(politica):
    webchat = ConfigWebchat("https://atendimento.exemplo.invalid/", "segredo-do-webchat")
    link = link_canal_externo(webchat, politica, 42, AGORA)

    mensagem = mensagem_fixa(TipoFixa.CANAL_EXTERNO, replace(DADOS, link_canal_externo=link))

    [botao] = mensagem.botoes
    assert botao.valor == link
    prefixo = "https://atendimento.exemplo.invalid/atendimento/"
    assert link.startswith(prefixo)
    assert ler_token("segredo-do-webchat", link.removeprefix(prefixo), AGORA) == 42
    validade = AGORA + timedelta(hours=politica.webchat_token_validade_horas)
    with pytest.raises(TokenInvalido):
        ler_token("segredo-do-webchat", link.removeprefix(prefixo), validade)


def test_sem_o_dominio_do_webchat_a_6a_mensagem_mantem_o_link_provisorio(politica):
    assert link_canal_externo(None, politica, 42, AGORA) == politica.link_canal_externo
