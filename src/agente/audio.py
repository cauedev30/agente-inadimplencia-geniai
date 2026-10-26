"""Os formatos de áudio que o agente transcreve e a duração, lida do arquivo (D43)."""

from collections.abc import Mapping
from typing import Final

# Os content-types que o Chatwoot serve para os formatos da API de transcrição, cada um com a
# extensão pela qual a API reconhece o formato. O áudio de voz do WhatsApp é audio/ogg (Opus).
FORMATOS_AUDIO: Final[Mapping[str, str]] = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "application/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}

# O Opus conta o grânulo em amostras a 48 kHz, qualquer que seja a taxa de entrada (RFC 7845).
TAXA_OPUS: Final = 48_000

_CABECALHO_PAGINA: Final = 27
_SEM_FIM_DE_PACOTE: Final = -1


def duracao_ogg_opus(dados: bytes) -> float | None:
    """Quanto dura um áudio Ogg Opus: o grânulo da última página do fluxo, menos o pre-skip do
    cabeçalho, sobre 48 000. None quando os dados não são um Ogg Opus inteiro e bem formado."""
    pos, serial, pre_skip, ultimo = 0, -1, 0, None
    while pos + _CABECALHO_PAGINA <= len(dados):
        if dados[pos : pos + 4] != b"OggS":
            return None
        segmentos = dados[pos + 26]
        inicio_corpo = pos + _CABECALHO_PAGINA + segmentos
        fim_corpo = inicio_corpo + sum(dados[pos + _CABECALHO_PAGINA : inicio_corpo])
        if fim_corpo > len(dados):
            return None
        granulo = int.from_bytes(dados[pos + 6 : pos + 14], "little", signed=True)
        serial_da_pagina = int.from_bytes(dados[pos + 14 : pos + 18], "little")
        if serial == -1:
            cabeca = dados[inicio_corpo:fim_corpo]
            if not cabeca.startswith(b"OpusHead") or len(cabeca) < 12:
                return None
            serial, pre_skip = serial_da_pagina, int.from_bytes(cabeca[10:12], "little")
        elif serial_da_pagina == serial and granulo != _SEM_FIM_DE_PACOTE:
            ultimo = granulo
        pos = fim_corpo
    if pos != len(dados) or ultimo is None:
        return None
    return max(0, ultimo - pre_skip) / TAXA_OPUS


def duracao_segundos(dados: bytes, content_type: str) -> float | None:
    """A duração de um áudio Ogg; None para os outros formatos, cuja duração não é lida."""
    if FORMATOS_AUDIO.get(content_type) != "ogg":
        return None
    return duracao_ogg_opus(dados)
