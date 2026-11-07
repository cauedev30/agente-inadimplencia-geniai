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
