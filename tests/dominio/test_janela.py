from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from agente.dominio.janela import dentro_da_janela, proxima_abertura

JP = ZoneInfo("America/Fortaleza")


def _jp(ano, mes, dia, hora, minuto=0):
    return datetime(ano, mes, dia, hora, minuto, tzinfo=JP)


@pytest.mark.parametrize(
    ("momento", "dentro"),
    [
        (_jp(2026, 10, 26, 7, 59), False),  # segunda
        (_jp(2026, 10, 26, 8, 0), True),
        (_jp(2026, 10, 26, 17, 59), True),
        (_jp(2026, 10, 26, 18, 0), False),
        (_jp(2026, 10, 31, 11, 59), True),  # sábado
        (_jp(2026, 10, 31, 12, 0), False),
        (_jp(2026, 11, 1, 10, 0), False),  # domingo
        (_jp(2026, 11, 2, 10, 0), False),  # Finados, segunda
    ],
)
def test_dentro_da_janela(politica, momento, dentro):
    assert dentro_da_janela(momento, politica) is dentro


def test_janela_usa_o_fuso_de_joao_pessoa(politica):
    # 10h50 UTC são 7h50 em João Pessoa: ainda fechado.
    assert dentro_da_janela(datetime(2026, 10, 26, 10, 50, tzinfo=UTC), politica) is False
    assert dentro_da_janela(datetime(2026, 10, 26, 11, 0, tzinfo=UTC), politica) is True


def test_horario_sem_fuso_e_recusado(politica):
    with pytest.raises(ValueError, match="fuso"):
        dentro_da_janela(datetime(2026, 10, 26, 10, 0), politica)


@pytest.mark.parametrize(
    ("momento", "abertura"),
    [
        (_jp(2026, 10, 26, 6, 30), _jp(2026, 10, 26, 8, 0)),  # antes de abrir
        (_jp(2026, 10, 30, 18, 30), _jp(2026, 10, 31, 8, 0)),  # sexta à noite → sábado
        (_jp(2026, 10, 31, 12, 30), _jp(2026, 11, 3, 8, 0)),  # sábado → terça (Finados)
        (_jp(2026, 11, 14, 12, 30), _jp(2026, 11, 16, 8, 0)),  # sábado → segunda
    ],
)
def test_proxima_abertura(politica, momento, abertura):
    assert proxima_abertura(momento, politica) == abertura


def test_dentro_da_janela_a_proxima_abertura_e_agora(politica):
    agora = _jp(2026, 10, 26, 10, 15)
    assert proxima_abertura(agora, politica) == agora


def test_sabado_sai_da_janela_pelo_arquivo(politica):
    dias = {dia: h for dia, h in politica.janela.dias.items() if dia != "sab"}
    sem_sabado = politica.model_copy(
        update={"janela": politica.janela.model_copy(update={"dias": dias})}
    )
    assert dentro_da_janela(_jp(2026, 10, 31, 10, 0), sem_sabado) is False
    assert proxima_abertura(_jp(2026, 10, 30, 18, 30), sem_sabado) == _jp(2026, 11, 3, 8, 0)
