"""Um Chatwoot falso, local, para o ensaio automático da demo.

Atende as rotas que o `ChatwootHttp` do serviço chama, sob
`/api/v1/accounts/{conta}/conversations/{id}/`, e grava tudo o que o agente fez, para o relatório
do ensaio mostrar as mensagens, as notas, as etiquetas e o status de cada passo. Só stdlib, só em
127.0.0.1: nada sai da máquina.

A conta errada e a rota desconhecida dão 404 e viram um registro `erro`, porque um 404 aqui quer
dizer que o serviço chamou algo que o Chatwoot real talvez não atenda.
"""

import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# O message_type de uma mensagem que saiu, na API do Chatwoot (0 é a que chegou).
_SAIDA = 1
_ROTA = re.compile(
    r"^/api/v1/accounts/(?P<conta>\d+)/conversations/(?P<conversa>\d+)/"
    r"(?P<recurso>messages|labels|assignments|toggle_status)$"
)


@dataclass(frozen=True)
class Registro:
    """O que o agente fez numa conversa.

    `tipo`: mensagem, nota, etiquetas (a lista inteira depois do POST), status, atribuicao
    ou erro (404). `momento` é o `time.perf_counter()` de quando chegou.
    """

    conversa_id: int
    tipo: str
    conteudo: Any
    momento: float


class ChatwootFalsoLocal:
    def __init__(self, conta: int, porta: int = 0):
        self._conta = conta
        self._porta = porta
        self._trava = threading.Lock()
        self._registros: list[Registro] = []
        self._mensagens: dict[int, list[dict]] = {}
        self._etiquetas: dict[int, list[str]] = {}
        self._proximo_id = 1
        self._servidor: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._servidor is None:
            raise RuntimeError("o Chatwoot falso não foi iniciado")
        return f"http://127.0.0.1:{self._servidor.server_address[1]}"

    @property
    def registros(self) -> list[Registro]:
        with self._trava:
            return list(self._registros)

    def registros_desde(self, momento: float) -> list[Registro]:
        """Os registros que chegaram depois de `momento` (um `time.perf_counter()`)."""
        return [r for r in self.registros if r.momento > momento]

    def etiquetas(self, conversa_id: int) -> list[str]:
        with self._trava:
            return list(self._etiquetas.get(conversa_id, []))

    def iniciar(self) -> None:
        self._servidor = ThreadingHTTPServer(("127.0.0.1", self._porta), self._criar_handler())
        # O intervalo curto faz o `parar()` voltar logo (o padrão do serve_forever é 0,5 s).
        self._thread = threading.Thread(
            target=self._servidor.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def parar(self) -> None:
        if self._servidor is None:
            return
        self._servidor.shutdown()
        self._servidor.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._servidor = None
        self._thread = None

    def _registrar(self, conversa_id: int, tipo: str, conteudo: Any) -> None:
        with self._trava:
            self._registros.append(Registro(conversa_id, tipo, conteudo, time.perf_counter()))

    def _tratar(self, metodo: str, caminho: str, corpo: dict) -> tuple[int, dict]:
        achado = _ROTA.match(caminho)
        if achado is None or int(achado["conta"]) != self._conta:
            self._registrar(0, "erro", f"404 {metodo} {caminho}")
            return 404, {"erro": "rota desconhecida"}
        conversa = int(achado["conversa"])
        recurso = achado["recurso"]
        if recurso == "messages":
            return self._mensagens_da_conversa(metodo, conversa, corpo)
        if recurso == "labels":
            return self._etiquetas_da_conversa(metodo, conversa, corpo)
        if metodo != "POST":
            self._registrar(0, "erro", f"404 {metodo} {caminho}")
            return 404, {"erro": "rota desconhecida"}
        if recurso == "assignments":
            self._registrar(conversa, "atribuicao", corpo.get("team_id"))
        else:
            self._registrar(conversa, "status", corpo.get("status"))
        return 200, {}

    def _mensagens_da_conversa(self, metodo: str, conversa: int, corpo: dict) -> tuple[int, dict]:
        if metodo == "GET":
            with self._trava:
                return 200, {"payload": list(self._mensagens.get(conversa, []))}
        privada = bool(corpo.get("private"))
        with self._trava:
            numero = self._proximo_id
            self._proximo_id += 1
            self._mensagens.setdefault(conversa, []).append(
                {
                    "id": numero,
                    "content": corpo.get("content") or "",
                    "message_type": _SAIDA,
                    "private": privada,
                }
            )
        self._registrar(conversa, "nota" if privada else "mensagem", corpo.get("content") or "")
        return 200, {"id": numero}

    def _etiquetas_da_conversa(self, metodo: str, conversa: int, corpo: dict) -> tuple[int, dict]:
        if metodo == "GET":
            return 200, {"payload": self.etiquetas(conversa)}
        novas = [str(e) for e in corpo.get("labels") or []]
        with self._trava:
            # O POST do Chatwoot real substitui a lista.
            self._etiquetas[conversa] = novas
        self._registrar(conversa, "etiquetas", list(novas))
        return 200, {"payload": novas}

    def _criar_handler(self) -> type[BaseHTTPRequestHandler]:
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def _responder(self, metodo: str) -> None:
                tamanho = int(self.headers.get("Content-Length") or 0)
                bruto = self.rfile.read(tamanho) if tamanho else b""
                try:
                    corpo = json.loads(bruto) if bruto else {}
                except json.JSONDecodeError:
                    corpo = {}
                if not isinstance(corpo, dict):
                    corpo = {}
                status, resposta = falso._tratar(metodo, self.path.split("?")[0], corpo)
                dados = json.dumps(resposta).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def do_GET(self) -> None:  # noqa: N802 (nome fixo do BaseHTTPRequestHandler)
                self._responder("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._responder("POST")

            def log_message(self, *_args) -> None:
                pass

        return Handler
