"""O áudio do cliente vale como texto no turno (D43).

Os gates rodam antes. Depois do `Seguir` e antes do Jev 1, os áudios do cliente que nenhum turno
tentou ainda são baixados, medidos e transcritos, e o resultado fica no próprio anexo (`resultado`:
`transcrito`, `longo_demais` ou `falhou`, e a `transcricao`), sem migração. Cada áudio é transcrito
uma vez só: o resultado é gravado na hora, e os turnos seguintes o usam.

O texto transcrito é dado do cliente e o endereço do áudio é do Chatwoot: nenhum dos dois vai ao
log, que traz só o tipo, o tamanho, a duração e o tempo.
"""

import logging
import time

from sqlalchemy.orm import Session

from agente.audio import FORMATOS_AUDIO, duracao_segundos
from agente.db.modelos import Conversa, Mensagem
from agente.integracoes.chatwoot import Chatwoot, FalhaDownload
from agente.integracoes.transcritor import Transcritor, normalizar_texto
from agente.politica import Politica

log = logging.getLogger(__name__)

TRANSCRITO = "transcrito"
LONGO_DEMAIS = "longo_demais"
FALHOU = "falhou"

ROTULO_LONGO_DEMAIS = "[áudio longo demais — não ouvido]"
ROTULO_NAO_OUVIDO = "[áudio — não foi possível ouvir]"


def _e_audio(anexo: dict) -> bool:
    return anexo.get("tipo") == "audio"


def rotulo_do_audio(anexo: dict) -> str:
    """Como o áudio aparece no texto que o Jev e o LLM leem."""
    resultado = anexo.get("resultado")
    if resultado == TRANSCRITO and anexo.get("transcricao"):
        return f'[áudio transcrito: "{anexo["transcricao"]}"]'
    if resultado == LONGO_DEMAIS:
        return ROTULO_LONGO_DEMAIS
    return ROTULO_NAO_OUVIDO


def texto_da_mensagem(mensagem: Mensagem) -> str:
    """O texto da mensagem e, depois dele, o rótulo de cada áudio. Outros anexos não entram."""
    partes = [mensagem.texto.strip()] if mensagem.texto.strip() else []
    partes += [rotulo_do_audio(anexo) for anexo in mensagem.anexos if _e_audio(anexo)]
    return " ".join(partes)


def audios_sem_resultado(mensagens: list[Mensagem]) -> list[tuple[Mensagem, int]]:
    """Cada áudio do cliente que nenhum turno tentou: a mensagem e a posição do anexo."""
    return [
        (mensagem, posicao)
        for mensagem in mensagens
        for posicao, anexo in enumerate(mensagem.anexos)
        if _e_audio(anexo) and "resultado" not in anexo
    ]


def longo_demais(dados: bytes, content_type: str, politica: Politica) -> bool:
    """Passou de `audio_max_segundos`; pelo tamanho, quando a duração não se lê do arquivo."""
    segundos = duracao_segundos(dados, content_type)
    if segundos is not None:
        return segundos > politica.audio_max_segundos
    return len(dados) > politica.audio_sem_duracao_max_bytes


def _ouvir(
    conversa_id: int, anexo: dict, chatwoot: Chatwoot, transcritor: Transcritor, politica: Politica
) -> dict:
    """Baixa e transcreve um áudio e devolve o anexo com o resultado. Nunca levanta."""
    url = anexo.get("url")
    baixado = (
        FalhaDownload("host")
        if not url
        else chatwoot.baixar_anexo(
            url,
            aceitos=FORMATOS_AUDIO.keys(),
            max_bytes=politica.audio_max_bytes,
            prazo_s=politica.audio_prazo_segundos,
        )
    )
    if isinstance(baixado, FalhaDownload):
        log.info(
            "áudio da conversa %s não baixado: %s (tipo %s, tamanho %s)",
            conversa_id,
            baixado.motivo,
            baixado.content_type,
            baixado.tamanho,
        )
        return {**anexo, "resultado": FALHOU, "motivo": f"download_{baixado.motivo}"}
    segundos = duracao_segundos(baixado.dados, baixado.content_type)
    log.info(
        "áudio da conversa %s baixado: tipo %s, %s bytes, %s s",
        conversa_id,
        baixado.content_type,
        len(baixado.dados),
        None if segundos is None else round(segundos, 1),
    )
    if longo_demais(baixado.dados, baixado.content_type, politica):
        log.info("áudio da conversa %s longo demais: não transcrito", conversa_id)
        return {**anexo, "resultado": LONGO_DEMAIS}
    inicio = time.perf_counter()
    transcricao = transcritor.transcrever(baixado.dados, baixado.content_type)
    ms = round((time.perf_counter() - inicio) * 1000)
    # Uma linha só: o rótulo mostra o texto entre aspas.
    texto = normalizar_texto(transcricao.texto or "", politica.transcricao_max_caracteres)
    if transcricao.resultado != TRANSCRITO or not texto:
        motivo = transcricao.motivo or "vazio"
        log.info("áudio da conversa %s não transcrito: %s (%s ms)", conversa_id, motivo, ms)
        return {**anexo, "resultado": FALHOU, "motivo": motivo}
    log.info("áudio da conversa %s transcrito em %s ms", conversa_id, ms)
    return {**anexo, "resultado": TRANSCRITO, "transcricao": texto}


def transcrever_audios(
    sessao: Session,
    conversa: Conversa,
    mensagens: list[Mensagem],
    chatwoot: Chatwoot,
    transcritor: Transcritor,
    politica: Politica,
) -> list[str]:
    """Transcreve os áudios que nenhum turno tentou e grava o resultado de cada um na hora.

    O commit vem logo depois: se o turno falhar e voltar para a fila, o áudio não é transcrito de
    novo. Devolve os textos transcritos agora, na ordem.
    """
    pendentes = audios_sem_resultado(mensagens)
    if not pendentes:
        return []
    transcritos = []
    for mensagem, posicao in pendentes:
        novo = _ouvir(conversa.id, mensagem.anexos[posicao], chatwoot, transcritor, politica)
        # Uma lista nova: o SQLAlchemy não vê a mudança dentro do JSON.
        mensagem.anexos = [novo if i == posicao else a for i, a in enumerate(mensagem.anexos)]
        if novo["resultado"] == TRANSCRITO:
            transcritos.append(novo["transcricao"])
    sessao.commit()
    return transcritos
