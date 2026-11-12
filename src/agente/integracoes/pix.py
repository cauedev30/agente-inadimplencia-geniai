"""Interface do Pix dinâmico do banco do Cartão de Todos (D22).

A implementação real depende da T1 (tarefa 6.4). Até lá, o sistema roda com o PixFalso.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from agente.dominio.dinheiro import formatar_reais


class StatusPix(StrEnum):
    PENDENTE = "PENDENTE"
    PAGO = "PAGO"
    EXPIRADO = "EXPIRADO"


@dataclass(frozen=True)
class CobrancaPix:
    txid: str
    valor_centavos: int
    br_code: str
    expira_em: datetime


class TxidRepetido(ValueError):
    """O banco já tem uma cobrança com este txid."""


class PixIndisponivel(Exception):
    """O banco não respondeu (fora do ar, prazo estourado ou erro 5xx). Toda implementação do
    `PixBanco` levanta esta exceção nessa falha; o turno a trata como integração fora do ar."""


class PixBanco(Protocol):
    def criar_cobranca(
        self, txid: str, valor_centavos: int, validade: timedelta, devedor_nome: str
    ) -> CobrancaPix: ...

    def consultar(self, txid: str) -> StatusPix: ...


def gerar_txid(cobranca_id: int) -> str:
    """Um txid novo a cada Pix: 33 caracteres alfanuméricos (o Bacen aceita de 26 a 35)."""
    return f"agi{cobranca_id:010d}{secrets.token_hex(10)}"


class PixFalso:
    def __init__(self, agora: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._agora = agora
        self.cobrancas: dict[str, CobrancaPix] = {}
        self.pagos: set[str] = set()
        # Liga a falha do banco: toda chamada levanta PixIndisponivel.
        self.fora_do_ar = False

    def _exigir_no_ar(self) -> None:
        if self.fora_do_ar:
            raise PixIndisponivel("Pix fora do ar (simulado)")

    def criar_cobranca(
        self, txid: str, valor_centavos: int, validade: timedelta, devedor_nome: str
    ) -> CobrancaPix:
        self._exigir_no_ar()
        if valor_centavos <= 0:
            raise ValueError(f"o valor do Pix tem de ser positivo, veio {valor_centavos}")
        if txid in self.cobrancas:
            raise TxidRepetido(txid)
        cobranca = CobrancaPix(
            txid=txid,
            valor_centavos=valor_centavos,
            br_code=f"00020126FALSO{txid}{valor_centavos}",
            expira_em=self._agora() + validade,
        )
        self.cobrancas[txid] = cobranca
        return cobranca

    def consultar(self, txid: str) -> StatusPix:
        self._exigir_no_ar()
        if txid in self.pagos:
            return StatusPix.PAGO
        if self._agora() > self.cobrancas[txid].expira_em:
            return StatusPix.EXPIRADO
        return StatusPix.PENDENTE

    def simular_pagamento(self, txid: str) -> None:
        if txid not in self.cobrancas:
            raise KeyError(f"não há cobrança com o txid {txid}")
        self.pagos.add(txid)


class PixDeTeste(PixFalso):
    """O Pix do modo teste (D56): o txid e o resto são os do falso, mas o "copia e cola" é um texto
    `[TESTE]` que diz o valor. Nada vai a um banco."""

    def criar_cobranca(
        self, txid: str, valor_centavos: int, validade: timedelta, devedor_nome: str
    ) -> CobrancaPix:
        cobranca = super().criar_cobranca(txid, valor_centavos, validade, devedor_nome)
        cobranca = replace(
            cobranca,
            br_code=f"[TESTE] aqui viria o código Pix de {formatar_reais(valor_centavos)}.",
        )
        self.cobrancas[txid] = cobranca
        return cobranca
