"""Os formatos de áudio aceitos e a duração, lida do arquivo sem biblioteca (tarefa 8.2)."""

import pytest

from agente.audio import FORMATOS_AUDIO, duracao_segundos
from tests.ogg_opus import ogg_opus


def test_o_audio_do_whatsapp_e_ogg():
    assert FORMATOS_AUDIO["audio/ogg"] == "ogg"
    assert FORMATOS_AUDIO["audio/opus"] == "ogg"
    assert FORMATOS_AUDIO["audio/mpeg"] == "mp3"
    assert "image/jpeg" not in FORMATOS_AUDIO


def test_a_duracao_de_um_ogg_opus_pequeno():
    assert duracao_segundos(ogg_opus(5), "audio/ogg") == pytest.approx(5.0)


def test_a_duracao_desconta_o_pre_skip():
    assert duracao_segundos(ogg_opus(2.5, pre_skip=3840), "audio/ogg") == pytest.approx(2.5)


def test_o_content_type_opus_tambem_le_a_duracao():
    assert duracao_segundos(ogg_opus(3), "audio/opus") == pytest.approx(3.0)


def test_formato_sem_duracao_legivel_devolve_none():
    assert duracao_segundos(ogg_opus(5), "audio/mpeg") is None
    assert duracao_segundos(b"ID3\x04" + b"\x00" * 100, "audio/mpeg") is None


@pytest.mark.parametrize(
    "dados",
    [
        b"",
        b"isto nao e ogg",
        ogg_opus(5)[:-10],  # cortado no meio da última página
        ogg_opus(5) + b"lixo",  # sobra no fim
        b"OggS" + b"\x00" * 30,  # sem o OpusHead
    ],
)
def test_ogg_malformado_devolve_none(dados):
    assert duracao_segundos(dados, "audio/ogg") is None
