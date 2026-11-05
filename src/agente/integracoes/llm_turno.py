"""A chamada JSON ao Luna do turno de uma chamada (D47).

Uma chamada por turno devolve os sinais, a ação e o texto num objeto JSON só. A API é a de chat
completions com `response_format: json_object`, a mesma do `RedatorOpenAI` (D42). Aqui só há o
transporte: o que o JSON significa é do `turno/uma_chamada.py`.
"""

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from agente.integracoes.redator import URL_OPENAI, Uso

# O Luna às vezes cerca o JSON com ```json ... ``` ou o escreve no meio de um texto, mesmo pedindo
# `json_object`: vale do primeiro `{` ao último `}`, como o `_extract_json` do outro agente.
_OBJETO = re.compile(r"\{.*\}", re.DOTALL)


class LLMTurnoIndisponivel(RuntimeError):
    """O LLM falhou (HTTP), demorou ou devolveu um conteúdo vazio."""


class JSONInvalido(ValueError):
    """O conteúdo devolvido não é um objeto JSON."""


class LLMTurno(Protocol):
    # Quem respondeu, para comparar os candidatos.
    nome: str
    # Os tokens da última chamada, para o custo; None quando não informa ou a chamada falhou.
    ultimo_uso: Uso | None

    def completar_json(self, sistema: str, usuario: str) -> dict:
        """O objeto JSON da resposta. Levanta LLMTurnoIndisponivel ou JSONInvalido."""
        ...


def ler_objeto_json(conteudo: str) -> dict:
    """O texto como objeto JSON; aceita o cercado por ```json. Levanta JSONInvalido."""
    texto = conteudo.strip()
    achado = _OBJETO.search(texto)
    if achado:
        texto = achado.group(0)
    try:
        dado = json.loads(texto)
    except ValueError:
        raise JSONInvalido("o conteúdo não é JSON") from None
    if not isinstance(dado, dict):
        raise JSONInvalido("o JSON não é um objeto")
    return dado


class LLMTurnoOpenAI:
    """A API de chat completions da OpenAI, ou de quem fale o mesmo protocolo, em modo JSON.

    O `corpo_extra` entra no corpo do pedido: o Luna só aceita a temperatura com o
    `reasoning_effort: "none"` (medido no outro agente).
    """

    def __init__(
        self,
        chave: str,
        modelo: str,
        *,
        url: str = URL_OPENAI,
        corpo_extra: dict | None = None,
        timeout: float = 15.0,
        temperatura: float = 0.3,
    ):
        self.modelo = modelo
        self.nome = f"openai:{modelo}"
        self.temperatura = temperatura
        self.corpo_extra = dict(corpo_extra or {})
        self.ultimo_uso: Uso | None = None
        self._cliente = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {chave}"},
            timeout=timeout,
        )

    def __enter__(self) -> "LLMTurnoOpenAI":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

    def completar_json(self, sistema: str, usuario: str) -> dict:
        # Uma chamada que falha não deixa o uso da anterior para a comparação somar de novo.
        self.ultimo_uso = None
        corpo = {
            "model": self.modelo,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            "temperature": self.temperatura,
            "response_format": {"type": "json_object"},
            **self.corpo_extra,
        }
        try:
            resposta = self._cliente.post("/chat/completions", json=corpo)
            resposta.raise_for_status()
            dados = resposta.json()
            conteudo = dados["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as erro:
            # Só o status: a mensagem do httpx não leva a chave, mas o corpo pode ecoar o pedido.
            raise LLMTurnoIndisponivel(f"o LLM respondeu {erro.response.status_code}") from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMTurnoIndisponivel(f"o LLM falhou: {e}") from None
        if not conteudo:
            raise LLMTurnoIndisponivel("o LLM devolveu um conteúdo vazio")
        objeto = ler_objeto_json(conteudo)
        uso = dados.get("usage") or {}
        self.ultimo_uso = Uso(uso.get("prompt_tokens", 0), uso.get("completion_tokens", 0))
        return objeto


@dataclass
class LLMTurnoFalso:
    """Devolve as `respostas` dadas, uma por chamada: um `dict` sai como está, uma `str` é lida como
    JSON (ou levanta `JSONInvalido`) e uma `Exception` é levantada. Quando acabam, levanta
    `LLMTurnoIndisponivel`.

    Só grava os pedidos com `registrar`, que é coisa de teste: o pedido traz o histórico do cliente.
    """

    respostas: Sequence[dict | str | Exception] = ()
    uso: Uso | None = None
    demora_s: float = 0.0
    nome: str = "falso"
    registrar: bool = False
    pedidos: list[tuple[str, str]] = field(default_factory=list)
    ultimo_uso: Uso | None = None
    _usadas: int = field(default=0, init=False, repr=False)

    def completar_json(self, sistema: str, usuario: str) -> dict:
        self.ultimo_uso = None
        if self.registrar:
            self.pedidos.append((sistema, usuario))
        if self.demora_s:
            time.sleep(self.demora_s)
        if self._usadas >= len(self.respostas):
            raise LLMTurnoIndisponivel("sem resposta na falsa")
        resposta = self.respostas[self._usadas]
        self._usadas += 1
        if isinstance(resposta, Exception):
            raise resposta
        objeto = resposta if isinstance(resposta, dict) else ler_objeto_json(resposta)
        self.ultimo_uso = self.uso
        return objeto
