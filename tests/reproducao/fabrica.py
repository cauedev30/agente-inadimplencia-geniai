"""Um Chatwoot simulado para os testes da reprodução: payloads no formato real da API (confirmado
em 2026-10-30), com dados inventados. O servidor só atende GET e grava cada pedido.
"""

import json

import httpx

CONTA = 7
INBOX = 198
TOKEN = "segredo-de-teste"
TELEFONE = "+5583999990001"
# Segunda-feira, 10h em João Pessoa (13h UTC): dentro da janela de atendimento.
SEGUNDA_10H = 1793019600


def mensagem(
    id: int,
    conversa_id: int,
    tipo: int,
    texto: str,
    criada: int,
    *,
    remetente: str | None = None,
    privada: bool = False,
    anexos: list[dict] | None = None,
) -> dict:
    """Uma mensagem como o `GET .../messages` devolve. A de atividade (tipo 2) não traz `sender`."""
    dados = {
        "id": id,
        "content": texto,
        "inbox_id": INBOX,
        "conversation_id": conversa_id,
        "message_type": tipo,
        "content_type": "text",
        "status": "sent",
        "content_attributes": {},
        "created_at": criada,
        "private": privada,
        "source_id": None,
    }
    if tipo != 2:
        dados["sender"] = {
            "id": 1,
            "name": "Fulano Inventado",
            "type": remetente or ("contact" if tipo == 0 else "user"),
        }
    if anexos:
        dados["attachments"] = anexos
    return dados


def anexo(tipo: str = "audio", message_id: int = 0) -> dict:
    return {
        "id": 1,
        "message_id": message_id,
        "file_type": tipo,
        "account_id": CONTA,
        "extension": None,
        "data_url": "https://chatwoot.invalid/arquivo-inventado",
        "thumb_url": "",
        "file_size": 1234,
    }


def conversa(id: int, atividade: int, *, telefone: str | None = TELEFONE, criada: int = 0) -> dict:
    """Uma conversa como a listagem devolve: o telefone do contato está em `meta.sender`."""
    contato = {"id": 9, "name": "Fulano Inventado", "phone_number": telefone}
    return {
        "id": id,
        "inbox_id": INBOX,
        "account_id": CONTA,
        "status": "open",
        "labels": [],
        "messages": [],
        "meta": {"sender": contato, "channel": "Channel::Whatsapp"},
        "created_at": criada or atividade - 600,
        "last_activity_at": atividade,
        "timestamp": atividade,
    }


class ChatwootSimulado:
    """Atende a listagem de conversas e as mensagens, com paginação, e grava os pedidos."""

    def __init__(self, conversas: list[dict], mensagens: dict[int, list[dict]], pagina: int = 2):
        self.conversas = conversas
        self.mensagens = mensagens
        self.pagina = pagina
        self.pedidos: list[httpx.Request] = []
        self.transporte = httpx.MockTransport(self._atender)

    def _atender(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        if pedido.method != "GET":
            return httpx.Response(405)
        if pedido.headers.get("api_access_token") != TOKEN:
            return httpx.Response(401)
        partes = pedido.url.path.removeprefix(f"/api/v1/accounts/{CONTA}/").split("/")
        parametros = pedido.url.params
        if partes == ["conversations"]:
            return self._listar(parametros)
        if len(partes) == 3 and partes[0] == "conversations" and partes[2] == "messages":
            return self._mensagens(int(partes[1]), parametros)
        return httpx.Response(404)

    def _listar(self, parametros) -> httpx.Response:
        da_inbox = [c for c in self.conversas if c["inbox_id"] == int(parametros["inbox_id"])]
        ordenadas = sorted(da_inbox, key=lambda c: c["last_activity_at"], reverse=True)
        inicio = (int(parametros.get("page", 1)) - 1) * self.pagina
        corpo = {"data": {"meta": {"all_count": len(ordenadas)}, "payload": []}}
        corpo["data"]["payload"] = ordenadas[inicio : inicio + self.pagina]
        return httpx.Response(200, content=json.dumps(corpo))

    def _mensagens(self, conversa_id: int, parametros) -> httpx.Response:
        todas = sorted(self.mensagens.get(conversa_id, []), key=lambda m: m["id"])
        if "before" in parametros:
            todas = [m for m in todas if m["id"] < int(parametros["before"])]
        corpo = {"meta": {}, "payload": todas[-self.pagina :]}
        return httpx.Response(200, content=json.dumps(corpo))

    @property
    def caminhos(self) -> list[str]:
        return [pedido.url.path for pedido in self.pedidos]
