import base64
from datetime import UTC, datetime, timedelta

import pytest

from agente.tipos import EstadoConversa, StatusCobranca
from agente.webchat.token import TokenInvalido, emitir_token, ler_token, validar_token

SEGREDO = "segredo-do-webchat"
AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
VALIDADE = AGORA + timedelta(days=30)


def test_o_token_volta_com_o_id_da_cobranca():
    token = emitir_token(SEGREDO, 4321, VALIDADE)

    assert ler_token(SEGREDO, token, AGORA) == 4321


def test_o_token_nao_mostra_o_id_em_claro():
    token = emitir_token(SEGREDO, 4321, VALIDADE)
    bruto = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

    assert "4321" not in token
    assert (4321).to_bytes(8, "big") not in bruto
    assert token.isascii() and "=" not in token


def test_o_mesmo_id_nao_da_sempre_o_mesmo_token():
    assert emitir_token(SEGREDO, 7, VALIDADE) != emitir_token(SEGREDO, 7, VALIDADE)


def test_o_token_adulterado_e_recusado():
    token = emitir_token(SEGREDO, 4321, VALIDADE)
    for posicao in (2, len(token) // 2, len(token) - 2):
        troca = "A" if token[posicao] != "A" else "B"
        adulterado = token[:posicao] + troca + token[posicao + 1 :]
        with pytest.raises(TokenInvalido):
            ler_token(SEGREDO, adulterado, AGORA)


@pytest.mark.parametrize("lixo", ["", "abc", "!!!!", "A" * 60, "ç" * 10])
def test_o_lixo_e_recusado(lixo):
    with pytest.raises(TokenInvalido):
        ler_token(SEGREDO, lixo, AGORA)


def test_o_token_de_outro_segredo_e_recusado():
    token = emitir_token("outro-segredo", 4321, VALIDADE)

    with pytest.raises(TokenInvalido):
        ler_token(SEGREDO, token, AGORA)


def test_o_token_vencido_e_recusado():
    token = emitir_token(SEGREDO, 4321, VALIDADE)

    assert ler_token(SEGREDO, token, VALIDADE - timedelta(seconds=1)) == 4321
    with pytest.raises(TokenInvalido):
        ler_token(SEGREDO, token, VALIDADE)


def test_segredo_vazio_nao_emite_nem_valida():
    with pytest.raises(ValueError, match="segredo"):
        emitir_token("", 1, VALIDADE)
    token = emitir_token(SEGREDO, 1, VALIDADE)
    with pytest.raises(TokenInvalido):
        ler_token("", token, AGORA)


def test_a_validade_precisa_de_fuso():
    with pytest.raises(ValueError, match="fuso"):
        emitir_token(SEGREDO, 1, datetime(2026, 11, 1))


def test_a_cobranca_ativa_valida(conversa, sessao):
    cobranca = conversa.cobranca
    token = emitir_token(SEGREDO, cobranca.id, VALIDADE)

    assert validar_token(sessao, SEGREDO, token, AGORA) == cobranca.id


@pytest.mark.parametrize("status", [StatusCobranca.PAGA, StatusCobranca.ENCERRADA])
def test_a_cobranca_encerrada_revoga_o_token(conversa, sessao, status):
    cobranca = conversa.cobranca
    token = emitir_token(SEGREDO, cobranca.id, VALIDADE)
    cobranca.status = status
    sessao.commit()

    with pytest.raises(TokenInvalido):
        validar_token(sessao, SEGREDO, token, AGORA)


@pytest.mark.parametrize("estado", [EstadoConversa.ENCERRADO, EstadoConversa.PAGO])
def test_a_conversa_encerrada_revoga_o_token(conversa, sessao, estado):
    token = emitir_token(SEGREDO, conversa.cobranca.id, VALIDADE)
    conversa.estado = estado
    sessao.commit()

    with pytest.raises(TokenInvalido):
        validar_token(sessao, SEGREDO, token, AGORA)


def test_o_atendimento_humano_nao_revoga(conversa, sessao):
    token = emitir_token(SEGREDO, conversa.cobranca.id, VALIDADE)
    conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
    sessao.commit()

    assert validar_token(sessao, SEGREDO, token, AGORA) == conversa.cobranca.id


def test_a_cobranca_que_nao_existe_e_recusada(sessao):
    token = emitir_token(SEGREDO, 999, VALIDADE)

    with pytest.raises(TokenInvalido):
        validar_token(sessao, SEGREDO, token, AGORA)
