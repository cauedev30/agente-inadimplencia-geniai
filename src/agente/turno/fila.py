"""Fila dos turnos, gravada no banco para sobreviver a um reinício.

Cada mensagem do cliente empurra o turno da conversa para `agora + rajada_segundos`; o turno só roda
quando o cliente para de escrever, e junta a rajada inteira.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agente.db.modelos import Conversa, Mensagem
from agente.tipos import OrigemMensagem

log = logging.getLogger(__name__)

Processar = Callable[[Session, int, datetime], None]
# Uma tarefa periódica do laço (o Pix vencido, por exemplo): roda a cada ciclo, em sessão própria.
Rotina = Callable[[Session, datetime], object]


def a_cada(intervalo: timedelta, rotina: Rotina) -> Rotina:
    """A rotina que roda no máximo uma vez por intervalo, mesmo que o laço gire a cada segundo."""
    ultima: datetime | None = None

    def espacada(sessao: Session, agora: datetime) -> object:
        nonlocal ultima
        if ultima is not None and agora - ultima < intervalo:
            return None
        ultima = agora
        return rotina(sessao, agora)

    espacada.__name__ = getattr(rotina, "__name__", "rotina")
    return espacada


def agendar_turno(sessao: Session, conversa_id: int, quando: datetime) -> None:
    sessao.get_one(Conversa, conversa_id).turno_em = quando
    sessao.flush()


def turnos_vencidos(sessao: Session, agora: datetime) -> list[int]:
    consulta = (
        select(Conversa.id)
        .where(Conversa.turno_em.is_not(None), Conversa.turno_em <= agora)
        .order_by(Conversa.turno_em)
    )
    return list(sessao.scalars(consulta))


def mensagens_pendentes(sessao: Session, conversa_id: int) -> list[Mensagem]:
    """As mensagens do cliente que nenhum turno consumiu ainda, na ordem em que chegaram."""
    conversa = sessao.get_one(Conversa, conversa_id)
    consulta = (
        select(Mensagem)
        .where(
            Mensagem.conversa_id == conversa_id,
            Mensagem.origem == OrigemMensagem.CLIENTE,
            Mensagem.id > (conversa.ultima_tomada_id or 0),
        )
        .order_by(Mensagem.id)
    )
    return list(sessao.scalars(consulta))


def tomar_turno(sessao: Session, conversa_id: int) -> list[Mensagem]:
    """Tira o turno da fila e devolve as mensagens dele, marcando-as como consumidas."""
    mensagens = mensagens_pendentes(sessao, conversa_id)
    conversa = sessao.get_one(Conversa, conversa_id)
    conversa.turno_em = None
    if mensagens:
        conversa.ultima_tomada_id = mensagens[-1].id
    sessao.flush()
    return mensagens


def processar_sem_acao(sessao: Session, conversa_id: int, agora: datetime) -> None:
    """Processamento provisório até o turno existir (tarefas 3.5 e 4.5): só consome a rajada."""
    mensagens = tomar_turno(sessao, conversa_id)
    log.info("turno da conversa %s com %s mensagens, sem ação", conversa_id, len(mensagens))


class Trabalhador:
    """Laço no próprio processo que roda os turnos vencidos."""

    def __init__(
        self,
        fabrica: sessionmaker[Session],
        processar: Processar,
        relogio: Callable[[], datetime],
        intervalo_segundos: float = 1.0,
        rotinas: Sequence[Rotina] = (),
    ):
        self._rotinas = tuple(rotinas)
        self._fabrica = fabrica
        self._processar = processar
        self._relogio = relogio
        self._intervalo = intervalo_segundos
        self._parar = threading.Event()
        self._fio: threading.Thread | None = None

    def rodar_uma_vez(self) -> int:
        agora = self._relogio()
        with self._fabrica() as sessao:
            vencidos = turnos_vencidos(sessao, agora)
        for conversa_id in vencidos:
            with self._fabrica() as sessao:
                try:
                    self._processar(sessao, conversa_id, agora)
                    sessao.commit()
                except Exception:  # um turno que falha não para os outros
                    sessao.rollback()
                    log.exception("o turno da conversa %s falhou", conversa_id)
        self._rodar_rotinas(agora)
        return len(vencidos)

    def _rodar_rotinas(self, agora: datetime) -> None:
        for rotina in self._rotinas:
            with self._fabrica() as sessao:
                try:
                    rotina(sessao, agora)
                    sessao.commit()
                except Exception:  # uma rotina que falha não para o laço
                    sessao.rollback()
                    log.exception("a rotina %s falhou", getattr(rotina, "__name__", rotina))

    def _laco(self) -> None:
        while not self._parar.wait(self._intervalo):
            try:
                self.rodar_uma_vez()
            except Exception:
                log.exception("o laço de turnos falhou; tenta de novo no próximo ciclo")

    def iniciar(self) -> None:
        self._fio = threading.Thread(target=self._laco, name="turnos", daemon=True)
        self._fio.start()

    def parar(self) -> None:
        self._parar.set()
        if self._fio is not None:
            self._fio.join(timeout=5)
