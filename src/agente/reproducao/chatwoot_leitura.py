"""Leitura do Chatwoot para a pré-sombra (etapa 10): as conversas de uma inbox e as mensagens delas.

A classe só lê: o único método HTTP que ela chama é o GET, e não há método de escrita. O token é de
um usuário de agente só da inbox e vai no cabeçalho `api_access_token`; nunca no endereço nem no
log.

O formato é o da API real (confirmado em 2026-10-30 com 30 conversas): o telefone do contato está
em `meta.sender.phone_number` da conversa, e a mensagem traz `message_type` numérico.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

# Sem este teto, uma API que repete a página para sempre travaria a leitura.
_MAX_PAGINAS = 1000


@dataclass(frozen=True)
class ConversaLida:
    """O que a reprodução precisa de uma conversa. O telefone vem como o Chatwoot o guarda."""

    id: int
    conta_id: int | None
    inbox_id: int | None
    telefone: str | None
    criada_em: datetime
    ultima_atividade_em: datetime


def _instante(segundos: int | float) -> datetime:
    return datetime.fromtimestamp(segundos, UTC)


def _conversa_lida(bruta: dict) -> ConversaLida:
    contato = (bruta.get("meta") or {}).get("sender") or {}
    atividade = bruta.get("last_activity_at") or bruta.get("timestamp") or bruta["created_at"]
    return ConversaLida(
        id=int(bruta["id"]),
        conta_id=bruta.get("account_id"),
        inbox_id=bruta.get("inbox_id"),
        telefone=contato.get("phone_number") or None,
        criada_em=_instante(bruta["created_at"]),
        ultima_atividade_em=_instante(atividade),
    )


class ChatwootLeitura:
    def __init__(
        self,
        url: str,
        conta: int,
        token: str,
        timeout: float = 15.0,
        transporte: httpx.BaseTransport | None = None,
    ):
        self.conta = conta
        self._cliente = httpx.Client(
            base_url=f"{url.rstrip('/')}/api/v1/accounts/{conta}",
            headers={"api_access_token": token},
            timeout=timeout,
            transport=transporte,
        )

    def __enter__(self) -> "ChatwootLeitura":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

    def _get(self, caminho: str, parametros: dict) -> dict:
        # O único pedido desta classe: não há post, put nem delete.
        resposta = self._cliente.get(caminho, params=parametros)
        resposta.raise_for_status()
        return resposta.json()

    def conversas(self, inbox_id: int, desde: datetime) -> list[ConversaLida]:
        """As conversas da inbox com atividade a partir de `desde`, da mais recente à mais antiga.

        A listagem do Chatwoot vem pela atividade mais recente: a leitura para na primeira página em
        que todas as conversas são mais antigas que `desde`.
        """
        lidas: list[ConversaLida] = []
        for pagina in range(1, _MAX_PAGINAS + 1):
            corpo = self._get(
                "/conversations",
                # O assignee_type "all" é explícito: sem ele a API pode devolver só as do usuário.
                {
                    "inbox_id": inbox_id,
                    "status": "all",
                    "assignee_type": "all",
                    "page": pagina,
                },
            )
            brutas = (corpo.get("data") or {}).get("payload") or []
            if not brutas:
                break
            da_pagina = [_conversa_lida(bruta) for bruta in brutas]
            lidas += [c for c in da_pagina if c.ultima_atividade_em >= desde]
            if all(c.ultima_atividade_em < desde for c in da_pagina):
                break
        return lidas

    def mensagens(self, conversa_id: int) -> list[dict]:
        """Todas as mensagens da conversa, como a API as devolve, da mais antiga à mais nova.

        A API entrega a página mais recente; `before` pede as anteriores ao menor id já lido.
        """
        por_id: dict[int, dict] = {}
        antes: int | None = None
        for _ in range(_MAX_PAGINAS):
            parametros = {} if antes is None else {"before": antes}
            corpo = self._get(f"/conversations/{conversa_id}/messages", parametros)
            novas = {m["id"]: m for m in corpo.get("payload") or [] if m["id"] not in por_id}
            if not novas:
                break
            por_id |= novas
            antes = min(por_id)
        return sorted(por_id.values(), key=lambda m: (m["created_at"], m["id"]))
