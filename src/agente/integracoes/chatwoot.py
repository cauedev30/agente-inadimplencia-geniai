"""Interface do Chatwoot, onde o agente atende como Agent Bot (D1).

A conta, a inbox e o token dependem da T3. A ChatwootHttp fala com a API de aplicação do Chatwoot;
a ChatwootFalso grava as chamadas para os testes.

O download do anexo do cliente (o áudio, D43) é um resultado, nunca uma exceção, e o log nunca
leva o endereço nem o conteúdo.
"""

import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from agente.tipos import Botao

Status = Literal["open", "pending", "resolved"]
_STATUS = ("open", "pending", "resolved")
# O message_type de uma mensagem enviada ao cliente, na API do Chatwoot (0 é a que chegou).
_SAIDA = 1
# Os redirecionamentos que o download do anexo segue: o link do Chatwoot costuma levar ao
# armazenamento, que é outro endereço.
MAX_REDIRECIONAMENTOS = 3

Origem = tuple[str, str, int]


@dataclass(frozen=True)
class AnexoBaixado:
    """O arquivo em memória, com o content-type que o servidor declarou (sem o charset)."""

    dados: bytes
    content_type: str


@dataclass(frozen=True)
class FalhaDownload:
    """Por que o anexo não veio, para o log: nunca o endereço nem os bytes."""

    motivo: Literal["host", "redirects", "timeout", "error", "status", "type", "size", "empty"]
    content_type: str | None = None
    tamanho: int | None = None


class Chatwoot(Protocol):
    def enviar_mensagem(
        self, conversa_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> str: ...

    def mensagens_do_agente(self, conversa_id: int) -> list[tuple[str, str]]:
        """O id e o texto das últimas mensagens que o agente mandou ao cliente (sem as notas)."""
        ...

    def nota_privada(self, conversa_id: int, texto: str) -> None: ...

    def etiquetar(self, conversa_id: int, etiquetas: Sequence[str]) -> None: ...

    def atribuir(self, conversa_id: int, time_id: int) -> None: ...

    def mudar_status(self, conversa_id: int, status: Status) -> None: ...

    def gravar_atributos(self, conversa_id: int, dados: dict) -> None: ...

    def baixar_anexo(
        self, url: str, *, aceitos: Collection[str], max_bytes: int, prazo_s: float
    ) -> AnexoBaixado | FalhaDownload:
        """Baixa o anexo de um cliente. Nunca levanta: a falha é um resultado.

        Só o endereço da origem do Chatwoot é aberto, e só ele recebe o token.
        """
        ...


def _origem(url: str) -> Origem | None:
    """Esquema, host e porta de um endereço http(s) absoluto; None para qualquer outro."""
    partes = urlsplit(url)
    esquema = partes.scheme.lower()
    if esquema not in ("http", "https") or not partes.hostname:
        return None
    try:
        porta = partes.port
    except ValueError:
        return None
    return esquema, partes.hostname.lower(), porta or (443 if esquema == "https" else 80)


def _exigir_status(status: str) -> None:
    if status not in _STATUS:
        raise ValueError(f"status do Chatwoot desconhecido: {status}")


class ChatwootHttp:
    def __init__(self, url: str, conta: int, token: str, timeout: float = 10.0):
        self._cliente = httpx.Client(
            base_url=f"{url.rstrip('/')}/api/v1/accounts/{conta}",
            headers={"api_access_token": token},
            timeout=timeout,
        )
        # O download dos anexos tem cliente próprio, sem o token nos cabeçalhos padrão: o token vai
        # só nos pedidos à origem do Chatwoot (ver `_seguir`).
        self._token = token
        self._origem = _origem(url)
        self._anexos = httpx.Client(timeout=timeout)

    def __enter__(self) -> "ChatwootHttp":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()
        self._anexos.close()

    def _post(self, conversa_id: int, caminho: str, corpo: dict) -> dict:
        resposta = self._cliente.post(f"/conversations/{conversa_id}/{caminho}", json=corpo)
        resposta.raise_for_status()
        return resposta.json()

    def enviar_mensagem(
        self, conversa_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> str:
        botoes = list(botoes or [])
        # O botão de link vai no texto: o Chatwoot só manda botões de resposta (input_select).
        links = [f"{b.texto}: {b.valor}" for b in botoes if b.tipo == "url"]
        respostas = [b for b in botoes if b.tipo == "resposta"]
        corpo: dict = {
            "content": f"{texto}\n\n" + "\n".join(links) if links else texto,
            "message_type": "outgoing",
            "private": False,
        }
        if respostas:
            corpo["content_type"] = "input_select"
            corpo["content_attributes"] = {
                "items": [{"title": b.texto, "value": b.valor} for b in respostas]
            }
        return str(self._post(conversa_id, "messages", corpo)["id"])

    def mensagens_do_agente(self, conversa_id: int) -> list[tuple[str, str]]:
        resposta = self._cliente.get(f"/conversations/{conversa_id}/messages")
        if resposta.status_code in (401, 403):
            # O token do Agent Bot não lê as mensagens: 401 no Chatwoot real, conferido em
            # 2026-11-11; sem conferir, manda.
            return []
        resposta.raise_for_status()
        return [
            (str(mensagem["id"]), mensagem.get("content") or "")
            for mensagem in resposta.json().get("payload", [])
            # message_type 1 é a mensagem que saiu; a nota privada também sai, mas com private.
            if mensagem.get("message_type") == _SAIDA and not mensagem.get("private")
        ]

    def nota_privada(self, conversa_id: int, texto: str) -> None:
        corpo = {"content": texto, "message_type": "outgoing", "private": True}
        self._post(conversa_id, "messages", corpo)

    def etiquetar(self, conversa_id: int, etiquetas: Sequence[str]) -> None:
        # O POST de etiquetas substitui a lista: soma às que a conversa já tem.
        resposta = self._cliente.get(f"/conversations/{conversa_id}/labels")
        resposta.raise_for_status()
        atuais = list(resposta.json().get("payload", []))
        novas = atuais + [e for e in etiquetas if e not in atuais]
        self._post(conversa_id, "labels", {"labels": novas})

    def atribuir(self, conversa_id: int, time_id: int) -> None:
        self._post(conversa_id, "assignments", {"team_id": time_id})

    def mudar_status(self, conversa_id: int, status: Status) -> None:
        _exigir_status(status)
        self._post(conversa_id, "toggle_status", {"status": status})

    def gravar_atributos(self, conversa_id: int, dados: dict) -> None:
        self._post(conversa_id, "custom_attributes", {"custom_attributes": dados})

    def baixar_anexo(
        self, url: str, *, aceitos: Collection[str], max_bytes: int, prazo_s: float
    ) -> AnexoBaixado | FalhaDownload:
        """Só o endereço da origem configurada do Chatwoot (mesmo esquema, host e porta) é baixado.

        O token vai só a essa origem: o link do Chatwoot costuma redirecionar para o armazenamento,
        outro host, que não o recebe. No máximo `MAX_REDIRECIONAMENTOS`, nunca de https para http. O
        tamanho é lido em pedaços e o download para ao passar do limite; o prazo vale para tudo.
        """
        if self._origem is None or _origem(url) != self._origem:
            return FalhaDownload("host")
        prazo = time.monotonic() + prazo_s
        try:
            return self._seguir(url, aceitos, max_bytes, prazo)
        except httpx.TimeoutException:
            return FalhaDownload("timeout")
        except Exception:
            # Nem a mensagem do erro vai adiante: o httpx cita o endereço.
            return FalhaDownload("error")

    def _seguir(
        self, url: str, aceitos: Collection[str], max_bytes: int, prazo: float
    ) -> AnexoBaixado | FalhaDownload:
        for _ in range(MAX_REDIRECIONAMENTOS + 1):
            origem = _origem(url)
            cabecalhos = {"api_access_token": self._token} if origem == self._origem else {}
            restante = prazo - time.monotonic()
            if restante <= 0:
                return FalhaDownload("timeout")
            with self._anexos.stream("GET", url, headers=cabecalhos, timeout=restante) as resposta:
                if not resposta.is_redirect:
                    return self._ler(resposta, aceitos, max_bytes, prazo)
                url = urljoin(url, resposta.headers["location"])
            destino = _origem(url)
            if destino is None or (origem and origem[0] == "https" and destino[0] != "https"):
                return FalhaDownload("host")
        return FalhaDownload("redirects")

    def _ler(
        self, resposta: httpx.Response, aceitos: Collection[str], max_bytes: int, prazo: float
    ) -> AnexoBaixado | FalhaDownload:
        if not resposta.is_success:
            return FalhaDownload("status")
        content_type = resposta.headers.get("content-type", "").split(";")[0].strip().lower()
        bruto = resposta.headers.get("content-length", "")
        declarado = int(bruto) if bruto.isdigit() else None
        if content_type not in aceitos:
            return FalhaDownload("type", content_type or None, declarado)
        if declarado is not None and declarado > max_bytes:
            return FalhaDownload("size", content_type, declarado)
        dados = bytearray()
        for pedaco in resposta.iter_bytes():
            dados.extend(pedaco)
            if len(dados) > max_bytes:
                return FalhaDownload("size", content_type)
            if time.monotonic() > prazo:
                return FalhaDownload("timeout")
        if not dados:
            return FalhaDownload("empty", content_type, 0)
        return AnexoBaixado(bytes(dados), content_type)


class ChatwootFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []
        # O que o download devolve, e os endereços pedidos (fora de `chamadas`, que os testes
        # conferem inteira).
        self.anexo: AnexoBaixado | FalhaDownload = AnexoBaixado(b"audio-falso", "audio/ogg")
        self.baixados: list[str] = []

    def enviar_mensagem(
        self, conversa_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> str:
        self.chamadas.append(("enviar_mensagem", conversa_id, texto, list(botoes or [])))
        return f"falso-{len(self.chamadas)}"

    def mensagens_do_agente(self, conversa_id: int) -> list[tuple[str, str]]:
        return [
            (f"falso-{posicao}", chamada[2])
            for posicao, chamada in enumerate(self.chamadas, 1)
            if chamada[0] == "enviar_mensagem" and chamada[1] == conversa_id
        ]

    def nota_privada(self, conversa_id: int, texto: str) -> None:
        self.chamadas.append(("nota_privada", conversa_id, texto))

    def etiquetar(self, conversa_id: int, etiquetas: Sequence[str]) -> None:
        self.chamadas.append(("etiquetar", conversa_id, list(etiquetas)))

    def atribuir(self, conversa_id: int, time_id: int) -> None:
        self.chamadas.append(("atribuir", conversa_id, time_id))

    def mudar_status(self, conversa_id: int, status: Status) -> None:
        _exigir_status(status)
        self.chamadas.append(("mudar_status", conversa_id, status))

    def gravar_atributos(self, conversa_id: int, dados: dict) -> None:
        self.chamadas.append(("gravar_atributos", conversa_id, dict(dados)))

    def baixar_anexo(
        self, url: str, *, aceitos: Collection[str], max_bytes: int, prazo_s: float
    ) -> AnexoBaixado | FalhaDownload:
        self.baixados.append(url)
        return self.anexo

    def enviadas(self, conversa_id: int) -> list[str]:
        """Os textos enviados ao cliente nesta conversa (sem as notas privadas)."""
        return [c[2] for c in self.chamadas if c[0] == "enviar_mensagem" and c[1] == conversa_id]
