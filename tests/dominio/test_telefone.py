"""O telefone nos dois formatos do celular brasileiro, com e sem o 9 (A1)."""

import pytest

from agente.dominio.telefone import normalizar_telefone, variantes_telefone

COM_9 = "+5583988887777"
SEM_9 = "+558388887777"


def test_celular_com_9_devolve_os_dois_formatos():
    assert variantes_telefone(COM_9) == frozenset({COM_9, SEM_9})


def test_celular_sem_9_devolve_os_dois_formatos():
    assert variantes_telefone(SEM_9) == frozenset({COM_9, SEM_9})


@pytest.mark.parametrize("entrada", ["(83) 98888-7777", "83988887777", "+55 83 9 8888-7777"])
def test_aceita_o_telefone_em_qualquer_escrita(entrada):
    assert variantes_telefone(entrada) == frozenset({COM_9, SEM_9})


@pytest.mark.parametrize("fixo", ["+558333334444", "+558325554444", "+558352223333"])
def test_fixo_devolve_so_ele_mesmo(fixo):
    assert variantes_telefone(fixo) == frozenset({fixo})


def test_entrada_invalida_nao_tem_variante():
    assert normalizar_telefone("123") is None
    assert variantes_telefone("123") == frozenset()
    assert variantes_telefone("") == frozenset()
    assert variantes_telefone("+14155550100") == frozenset()


def test_onze_digitos_sem_9_na_frente_nao_inventa_variante():
    # DDD 83 e depois 2 (fixo) com 11 dígitos: não é celular, fica como veio.
    assert variantes_telefone("+5583288887777") == frozenset({"+5583288887777"})


@pytest.mark.parametrize("digito", "2345")
def test_nove_na_frente_de_numero_que_seria_fixo_nao_inventa_variante(digito):
    # +5511931234567: tirar o 9 daria +551131234567, um fixo de São Paulo, outra pessoa.
    numero = f"+55119{digito}1234567"
    assert variantes_telefone(numero) == frozenset({numero})


@pytest.mark.parametrize("digito", "6789")
def test_nove_na_frente_de_celular_de_verdade_continua_com_as_duas_escritas(digito):
    com_9 = f"+55119{digito}1234567"
    sem_9 = f"+5511{digito}1234567"
    assert variantes_telefone(com_9) == frozenset({com_9, sem_9})
