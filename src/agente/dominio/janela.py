"""Janela de atendimento (D11, D16): fora dela, o agente não responde nada."""

from datetime import date, datetime, timedelta

from agente.politica import Horario, Politica

_DIAS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
# Sem nenhum dia aberto em duas semanas, a política está errada.
_LIMITE_DIAS = 14


def _local(momento: datetime, politica: Politica) -> datetime:
    if momento.tzinfo is None:
        raise ValueError("horário sem fuso: a janela precisa saber o fuso do momento")
    return momento.astimezone(politica.janela.zona)


def _horario_do_dia(dia: date, politica: Politica) -> Horario | None:
    if dia in politica.janela.feriados:
        return None
    return politica.janela.dias.get(_DIAS[dia.weekday()])


def dentro_da_janela(momento: datetime, politica: Politica) -> bool:
    local = _local(momento, politica)
    horario = _horario_do_dia(local.date(), politica)
    return horario is not None and horario.inicio <= local.time() < horario.fim


def proxima_abertura(momento: datetime, politica: Politica) -> datetime:
    """O primeiro instante, a partir de `momento`, em que a janela está aberta."""
    local = _local(momento, politica)
    for adiante in range(_LIMITE_DIAS):
        dia = local.date() + timedelta(days=adiante)
        horario = _horario_do_dia(dia, politica)
        if horario is None:
            continue
        abertura = datetime.combine(dia, horario.inicio, tzinfo=politica.janela.zona)
        if adiante > 0 or local < abertura:
            return abertura
        if local.time() < horario.fim:
            return local
    raise ValueError(f"a janela não abre nos próximos {_LIMITE_DIAS} dias")
