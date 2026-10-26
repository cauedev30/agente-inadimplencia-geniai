"""Token do link do webchat: opaco, assinado e com validade (tarefa 7.1, ideia-webchat seção 4).

O link carrega só o token. Ele vai cifrado e autenticado (cifra de fluxo derivada do HMAC-SHA256 do
segredo, mais um HMAC sobre o conjunto), então o id da cobrança e a validade não aparecem em claro.
A revogação não é um gancho no `confirmar_pagamento`: `validar_token` confere o estado da cobrança e
da conversa a cada uso, e assim cobre também as saídas que não passam pelo pagamento. "Fechada"
hoje significa "paga": nada mais encerra a cobrança (G7 de `docs/duvidas.md`).
"""

import base64
import binascii
import hashlib
import hmac
import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Cobranca, Conversa
from agente.tipos import EstadoConversa, StatusCobranca

_VERSAO = b"\x01"
_NONCE = 12
_DADOS = 16  # id da cobrança (8 bytes) e validade (8 bytes)
_TAG = 16
_ENCERRADAS = (EstadoConversa.ENCERRADO, EstadoConversa.PAGO)


class TokenInvalido(Exception):
    """Adulterado, vencido, revogado ou de cobrança que não existe. Não diz qual: vira 404."""


def _chave(segredo: str, uso: bytes) -> bytes:
    return hmac.new(segredo.encode(), b"webchat:" + uso, hashlib.sha256).digest()


def _fluxo(segredo: str, nonce: bytes) -> bytes:
    return hmac.new(_chave(segredo, b"cifra"), nonce, hashlib.sha256).digest()[:_DADOS]


def _tag(segredo: str, corpo: bytes) -> bytes:
    return hmac.new(_chave(segredo, b"tag"), corpo, hashlib.sha256).digest()[:_TAG]


def emitir_token(segredo: str, cobranca_id: int, expira_em: datetime) -> str:
    if not segredo.strip():
        raise ValueError("o segredo do webchat está vazio")
    if expira_em.tzinfo is None:
        raise ValueError("a validade do token precisa do fuso horário")
    dados = cobranca_id.to_bytes(8, "big") + int(expira_em.timestamp()).to_bytes(8, "big")
    nonce = os.urandom(_NONCE)
    cifrado = bytes(a ^ b for a, b in zip(dados, _fluxo(segredo, nonce), strict=True))
    corpo = _VERSAO + nonce + cifrado
    return base64.urlsafe_b64encode(corpo + _tag(segredo, corpo)).rstrip(b"=").decode()


def ler_token(segredo: str, token: str, agora: datetime) -> int:
    """O id da cobrança de um token íntegro e dentro da validade (sem olhar o banco)."""
    if not segredo.strip():
        raise TokenInvalido
    try:
        # Só o texto canônico vale: sem o alfabeto padrão (+ e /), sem "=" e sem lixo no meio.
        bruto = base64.b64decode(
            token.replace("-", "+").replace("_", "/").encode("ascii") + b"=" * (-len(token) % 4),
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error):
        raise TokenInvalido from None
    if base64.urlsafe_b64encode(bruto).rstrip(b"=").decode() != token:
        raise TokenInvalido
    if len(bruto) != len(_VERSAO) + _NONCE + _DADOS + _TAG:
        raise TokenInvalido
    corpo, tag = bruto[:-_TAG], bruto[-_TAG:]
    # Tempo constante: a tag só é conferida antes de qualquer leitura do conteúdo.
    if not hmac.compare_digest(tag, _tag(segredo, corpo)) or corpo[:1] != _VERSAO:
        raise TokenInvalido
    nonce, cifrado = corpo[1 : 1 + _NONCE], corpo[1 + _NONCE :]
    dados = bytes(a ^ b for a, b in zip(cifrado, _fluxo(segredo, nonce), strict=True))
    if agora.timestamp() >= int.from_bytes(dados[8:], "big"):
        raise TokenInvalido
    return int.from_bytes(dados[:8], "big")


def validar_token(sessao: Session, segredo: str, token: str, agora: datetime) -> int:
    """O id da cobrança, se o token vale e a cobrança segue aberta; senão levanta TokenInvalido."""
    cobranca_id = ler_token(segredo, token, agora)
    cobranca = sessao.get(Cobranca, cobranca_id)
    if cobranca is None or cobranca.status is not StatusCobranca.ATIVA:
        raise TokenInvalido
    ultima = sessao.scalar(
        select(Conversa).where(Conversa.cobranca_id == cobranca_id).order_by(Conversa.id.desc())
    )
    if ultima is not None and ultima.estado in _ENCERRADAS:
        raise TokenInvalido
    return cobranca_id
