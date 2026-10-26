"""O link do webchat que a 6ª mensagem leva (tarefa 7.4)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agente.tipos import TipoFixa
from agente.turno.catalogo import DadosFixa, mensagem_fixa
from agente.webchat.link import ConfigWebchat, link_canal_externo
from agente.webchat.token import TokenInvalido, ler_token

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)

DADOS = DadosFixa(
    nome="Maria",
    valor_centavos=10020,
    link_cartao="https://ctn.invalid/cartao/1",
    link_canal_externo="https://atendimento.invalid/x",
)


def test_a_6a_mensagem_leva_o_link_com_um_token_valido_para_a_cobranca(politica):
    webchat = ConfigWebchat(
        "https://atendimento.exemplo.invalid/", "segredo-do-webchat-com-32-caracteres-ou-mais"
    )
    link = link_canal_externo(webchat, politica, 42, AGORA)

    mensagem = mensagem_fixa(TipoFixa.CANAL_EXTERNO, replace(DADOS, link_canal_externo=link))

    [botao] = mensagem.botoes
    assert botao.valor == link
    prefixo = "https://atendimento.exemplo.invalid/atendimento/"
    assert link.startswith(prefixo)
    assert (
        ler_token("segredo-do-webchat-com-32-caracteres-ou-mais", link.removeprefix(prefixo), AGORA)
        == 42
    )
    validade = AGORA + timedelta(hours=politica.webchat_token_validade_horas)
    with pytest.raises(TokenInvalido):
        ler_token(
            "segredo-do-webchat-com-32-caracteres-ou-mais", link.removeprefix(prefixo), validade
        )


def test_sem_o_dominio_do_webchat_a_6a_mensagem_mantem_o_link_provisorio(politica):
    assert link_canal_externo(None, politica, 42, AGORA) == politica.link_canal_externo
