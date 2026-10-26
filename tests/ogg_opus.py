"""Um Ogg Opus mínimo, só com o que o `agente.audio` lê: a duração. Sem decodificador."""

import struct

SERIAL = 0xA1B2C3D4


def _pagina(corpo: bytes, *, granulo: int, sequencia: int, tipo: int) -> bytes:
    cabecalho = b"OggS" + struct.pack("<BBqIII", 0, tipo, granulo, SERIAL, sequencia, 0)
    return cabecalho + bytes([1, len(corpo)]) + corpo


def ogg_opus(segundos: float, *, pre_skip: int = 312) -> bytes:
    """Três páginas: o OpusHead, os comentários e uma de áudio que termina em `segundos`."""
    cabeca = b"OpusHead" + bytes([1, 1]) + struct.pack("<H", pre_skip)
    cabeca += struct.pack("<IhB", 48000, 0, 0)
    comentarios = b"OpusTags" + struct.pack("<I", 4) + b"ficc" + struct.pack("<I", 0)
    ultimo_granulo = round(segundos * 48_000) + pre_skip
    return (
        _pagina(cabeca, granulo=0, sequencia=0, tipo=2)
        + _pagina(comentarios, granulo=0, sequencia=1, tipo=0)
        + _pagina(b"\x00" * 40, granulo=ultimo_granulo, sequencia=2, tipo=4)
    )
