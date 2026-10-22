"""Interface do disparador da GeniAI, que envia os templates aprovados na Meta (D3).

A API real depende da T4. Até lá, a abertura sai pelo DisparadorFalso.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agente.tipos import Botao


class Disparador(Protocol):
    def enviar_template(
        self, telefone_e164: str, template: str, variaveis: dict[str, str], botoes: Sequence[Botao]
    ) -> str:
        """Envia o template e devolve o id do envio. Levanta exceção se o envio falhar."""
        ...


@dataclass(frozen=True)
class Envio:
    telefone: str
    template: str
    variaveis: dict[str, str]
    botoes: list[Botao]


class FalhaNoEnvio(RuntimeError):
    pass


class DisparadorFalso:
    def __init__(self, falhar_para: set[str] | None = None):
        self.envios: list[Envio] = []
        self.falhar_para = set(falhar_para or ())

    def enviar_template(
        self, telefone_e164: str, template: str, variaveis: dict[str, str], botoes: Sequence[Botao]
    ) -> str:
        if telefone_e164 in self.falhar_para:
            raise FalhaNoEnvio(f"envio simulado com falha para {telefone_e164[-4:]}")
        self.envios.append(Envio(telefone_e164, template, dict(variaveis), list(botoes)))
        return f"disparo-{len(self.envios)}"
