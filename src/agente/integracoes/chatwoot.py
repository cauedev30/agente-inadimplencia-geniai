"""Interface do Chatwoot, onde o agente atende como Agent Bot (D1).

A conta, a inbox e o token dependem da T3. A ChatwootHttp fala com a API de aplicação do Chatwoot;
a ChatwootFalso grava as chamadas para os testes.
"""

from collections.abc import Sequence
from typing import Literal, Protocol

import httpx

from agente.tipos import Botao

Status = Literal["open", "pending", "resolved"]
_STATUS = ("open", "pending", "resolved")


class Chatwoot(Protocol):
    def enviar_mensagem(
        self, conversa_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> str: ...

    def nota_privada(self, conversa_id: int, texto: str) -> None: ...

    def etiquetar(self, conversa_id: int, etiquetas: Sequence[str]) -> None: ...

    def atribuir(self, conversa_id: int, time_id: int) -> None: ...

    def mudar_status(self, conversa_id: int, status: Status) -> None: ...

    def gravar_atributos(self, conversa_id: int, dados: dict) -> None: ...


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

    def __enter__(self) -> "ChatwootHttp":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

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


class ChatwootFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []

    def enviar_mensagem(
        self, conversa_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> str:
        self.chamadas.append(("enviar_mensagem", conversa_id, texto, list(botoes or [])))
        return f"falso-{len(self.chamadas)}"

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

    def enviadas(self, conversa_id: int) -> list[str]:
        """Os textos enviados ao cliente nesta conversa (sem as notas privadas)."""
        return [c[2] for c in self.chamadas if c[0] == "enviar_mensagem" and c[1] == conversa_id]
