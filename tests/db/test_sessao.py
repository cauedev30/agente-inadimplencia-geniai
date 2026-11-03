import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from agente.db.sessao import criar_motor

SENSIVEL = "texto inventado do cliente"
INSERT = text("INSERT INTO tabela_que_nao_existe (texto) VALUES (:t)")


def _erro(motor) -> str:
    with motor.connect() as conexao, pytest.raises(OperationalError) as achado:
        conexao.execute(INSERT, {"t": SENSIVEL})
    return str(achado.value)


def test_o_motor_comum_mantem_os_parametros_no_erro_do_sql():
    # Em produção o parâmetro ajuda a diagnosticar: o padrão não muda.
    assert SENSIVEL in _erro(criar_motor("sqlite://"))


def test_o_motor_que_oculta_os_parametros_nao_leva_dado_no_erro_do_sql():
    assert SENSIVEL not in _erro(criar_motor("sqlite://", ocultar_parametros=True))
