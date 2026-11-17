from datetime import UTC, datetime

import pytest

from agente.tipos import Botao, TipoFixa
from agente.turno.catalogo import (
    POLITICAS,
    TEXTO_ARQUIVO_RECEBIDO,
    TEXTO_CANAL_EXTERNO,
    Atendente,
    DadosFixa,
    Situacao,
    e_cortesia,
    mensagem_fixa,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)

DADOS = DadosFixa(
    nome="Maria",
    valor_centavos=10020,
    link_cartao="https://ctn.invalid/cartao/1",
)


@pytest.mark.parametrize(
    ("textos", "cortesia"),
    [
        (["Ok!"], True),
        (["Tá bom.", "obrigada"], True),
        (["👍"], True),
        (["ok, mas quanto fica?"], False),
        (["ok", "quanto fica?"], False),
        (["oi"], False),
        ([], False),
        (["Ok, obrigado!"], True),
        (["beleza, valeu 🙏"], True),
        (["ta bom obrigada"], True),
        (["obrigado", "[Sticker] [imagem enviada]"], True),
        (["muito obrigada"], False),
        (["bom dia"], False),
        (["?"], False),
    ],
)
def test_cortesia_so_quando_todas_as_mensagens_estao_na_lista(textos, cortesia):
    assert e_cortesia(textos) is cortesia


def test_canal_externo_diz_que_o_atendente_segue_na_conversa_e_nao_tem_botao():
    mensagem = mensagem_fixa(TipoFixa.CANAL_EXTERNO, DADOS)

    assert TEXTO_CANAL_EXTERNO == (
        "Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui "
        "mesmo, nesta conversa."
    )
    assert mensagem.texto == TEXTO_CANAL_EXTERNO
    assert mensagem.botoes == []


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
    sem_link = DadosFixa("Maria", 10020, None)
    with pytest.raises(ValueError, match="link_cartao"):
        mensagem_fixa(TipoFixa.CONFIRMACAO_CONVITE_CARTAO, sem_link)


def test_confirmacao_sem_valor_e_erro():
    sem_valor = DadosFixa("Maria", None, None)
    with pytest.raises(ValueError, match="valor"):
        mensagem_fixa(TipoFixa.CONFIRMACAO, sem_valor)


def test_humano_no_whatsapp_usa_o_texto_da_politica_e_nao_tem_botao():
    dados = DadosFixa("Maria", None, None, texto="Um atendente segue aqui.")

    mensagem = mensagem_fixa(TipoFixa.HUMANO_NO_WHATSAPP, dados)

    assert mensagem.texto == "Um atendente segue aqui."
    assert mensagem.botoes == []


def test_humano_no_whatsapp_sem_o_texto_e_erro():
    with pytest.raises(ValueError, match="texto"):
        mensagem_fixa(TipoFixa.HUMANO_NO_WHATSAPP, DadosFixa("Maria", None, None))


def test_a_foto_sem_texto_tem_a_frase_propria_com_o_atendente():
    saida = POLITICAS[Situacao.FOTO_SEM_TEXTO]

    assert isinstance(saida, Atendente)
    assert saida.frase == TEXTO_ARQUIVO_RECEBIDO
    assert TEXTO_ARQUIVO_RECEBIDO == (
        "Recebi o seu arquivo. Um atendente da unidade vai conferir e continuar com você aqui "
        "mesmo, nesta conversa."
    )


def test_so_a_foto_sem_texto_traz_frase_propria_entre_os_atendentes():
    com_frase = [
        situacao
        for situacao, saida in POLITICAS.items()
        if isinstance(saida, Atendente) and saida.frase
    ]

    assert com_frase == [Situacao.FOTO_SEM_TEXTO]
