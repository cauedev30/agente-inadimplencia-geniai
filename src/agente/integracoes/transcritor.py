"""Interface do transcritor: o áudio do cliente vira texto antes do Jev (D43).

O transcritor real é o `gpt-4o-mini-transcribe` da OpenAI. Ele nunca levanta exceção: a falha, o
tempo esgotado e o texto vazio são o resultado "falhou", com o motivo. O texto transcrito é dado do
cliente e nunca vai ao log; o motivo traz só o tipo da falha, nunca o corpo da resposta.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from agente.audio import FORMATOS_AUDIO

URL_TRANSCRICAO = "https://api.openai.com/v1"
MODELO_TRANSCRICAO = "gpt-4o-mini-transcribe"
# Os clientes falam português do Brasil: dizer o idioma deixa a transcrição mais rápida e melhor.
IDIOMA = "pt"
MAX_CARACTERES = 3000


@dataclass(frozen=True)
class Transcricao:
    """O resultado de uma transcrição. O `motivo` só existe quando falhou."""

    resultado: Literal["transcrito", "falhou"]
    texto: str | None = None
    motivo: str | None = None


class Transcritor(Protocol):
    def transcrever(self, dados: bytes, content_type: str) -> Transcricao:
        """Nunca levanta: a falha, o tempo esgotado e o texto vazio viram "falhou"."""
        ...


def normalizar_texto(texto: str, max_caracteres: int = MAX_CARACTERES) -> str:
    """O texto em uma linha só, cortado em `max_caracteres` e terminando em "…" se passou."""
    linha = " ".join(texto.split())
    if len(linha) <= max_caracteres:
        return linha
    return f"{linha[: max_caracteres - 1]}…"


def _falhou(motivo: str) -> Transcricao:
    return Transcricao("falhou", None, motivo)


class TranscritorOpenAI:
    """`POST {url}/audio/transcriptions`, multipart: o áudio em `file` (com a extensão pela qual a
    API reconhece o formato), o `model` e o `language`. A chave vem do
    AGENTE_TRANSCRICAO_API_KEY."""

    def __init__(
        self,
        chave: str,
        modelo: str,
        *,
        url: str = URL_TRANSCRICAO,
        timeout: float = 15.0,
        max_caracteres: int = MAX_CARACTERES,
    ):
        self.modelo = modelo
        self.max_caracteres = max_caracteres
        # Sem cookies e sem seguir redirecionamento: a chave não vai a outro endereço.
        self._cliente = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {chave}"},
            timeout=timeout,
            follow_redirects=False,
        )

    def __enter__(self) -> "TranscritorOpenAI":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

    def transcrever(self, dados: bytes, content_type: str) -> Transcricao:
        nome = f"audio.{FORMATOS_AUDIO.get(content_type, 'ogg')}"
        try:
            resposta = self._cliente.post(
                "/audio/transcriptions",
                files={"file": (nome, dados, content_type)},
                data={"model": self.modelo, "language": IDIOMA},
            )
        except httpx.TimeoutException:
            return _falhou("timeout")
        except Exception:
            # Nem a mensagem do erro vai adiante: o httpx cita o endereço.
            return _falhou("erro")
        if not resposta.is_success:
            return _falhou(f"status_{resposta.status_code}")
        try:
            texto = resposta.json()["text"]
        except Exception:
            return _falhou("erro")
        if not isinstance(texto, str):
            return _falhou("erro")
        texto = normalizar_texto(texto, self.max_caracteres)
        if not texto:
            return _falhou("vazio")
        return Transcricao("transcrito", texto)


@dataclass
class TranscritorFalso:
    """Transcreve o `texto` dado, ou falha com o `motivo`; as `respostas` valem uma por chamada.

    Cada resposta é um texto (transcrito) ou uma `Transcricao` pronta. Só guarda o tamanho e o tipo
    do áudio, nunca os bytes.
    """

    texto: str | None = None
    motivo: str = "falso"
    respostas: Sequence[str | Transcricao] = ()
    chamadas: list[tuple[int, str]] = field(default_factory=list)
    _usadas: int = field(default=0, init=False, repr=False)

    def transcrever(self, dados: bytes, content_type: str) -> Transcricao:
        self.chamadas.append((len(dados), content_type))
        if self._usadas < len(self.respostas):
            resposta = self.respostas[self._usadas]
            self._usadas += 1
            return Transcricao("transcrito", resposta) if isinstance(resposta, str) else resposta
        if self.texto is not None:
            return Transcricao("transcrito", self.texto)
        return _falhou(self.motivo)
