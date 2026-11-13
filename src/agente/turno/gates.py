"""Gates em código (arquitetura, seção 1): certezas que ficam fora do Jev.

O segredo, o JSON, a conta, a inbox e o dedupe já ficaram no webhook (tarefa 3.4). Aqui ficam os
gates do turno, nesta ordem: cobrança ativa, humano na conversa, reinício, anexo, figurinha,
janela de atendimento, janela de 24h do WhatsApp, teto, a cobrança quitada no CTN, a foto ou o
arquivo sem texto (vai ao humano) e o toque no botão "Pagar com Pix". O toque no botão é a única
mensagem que dispensa a janela de atendimento (D40).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import mensagens_no_teto
from agente.db.modelos import Conversa, Mensagem
from agente.dominio.janela import dentro_da_janela, proxima_abertura
from agente.dominio.telefone import variantes_telefone
from agente.politica import Politica
from agente.tipos import EstadoConversa, OrigemMensagem, StatusCobranca, TipoFixa

JANELA_WHATSAPP = timedelta(hours=24)


@dataclass(frozen=True)
class Seguir:
    """O turno segue para o Jev."""


@dataclass(frozen=True)
class Ignorar:
    motivo: str


@dataclass(frozen=True)
class Adiar:
    """Fora da janela de atendimento: o turno roda quando ela abre (D16)."""

    ate: datetime


@dataclass(frozen=True)
class ForaDas24h:
    """Passou das 24h do WhatsApp: só um template poderia sair, e o agente não o manda (AUD-04).
    O orquestrador leva a conversa ao humano, sem mensagem ao cliente."""


@dataclass(frozen=True)
class Fixa:
    tipo: TipoFixa


@dataclass(frozen=True)
class PagarPix:
    """O cliente tocou no botão "Pagar com Pix" (D26): vai direto para o GENERATE_PIX, sem o Jev."""


@dataclass(frozen=True)
class Reiniciar:
    """Comando de reinício, só para os telefones da equipe."""


@dataclass(frozen=True)
class Quitada:
    """A conferência de hoje achou a cobrança quitada no CTN: não se cobra nem se gera Pix. O
    orquestrador leva a conversa ao humano conferir (CONCILIACAO), uma vez só."""


@dataclass(frozen=True)
class ComprovanteSemTexto:
    """A rajada é só foto ou arquivo, sem texto (AUD-07): o Luna não os lê, então a conversa vai ao
    humano conferir (CONCILIACAO)."""


Gate = (
    Seguir
    | Ignorar
    | Adiar
    | ForaDas24h
    | Fixa
    | Reiniciar
    | PagarPix
    | Quitada
    | ComprovanteSemTexto
)


# O texto que o gateway do WhatsApp põe na mensagem que é só anexo: `[Image]`, `[Sticker]`,
# `[Document]`... Qualquer `[Palavra]` com anexo conta. A figurinha chega com anexo do tipo `image`,
# igual à foto: só o rótulo distingue as duas.
_ROTULO_DO_GATEWAY = re.compile(r"\[[A-Za-z]+\]")
_FIGURINHA = "[Sticker]"


def _rotulo_do_gateway(mensagem: Mensagem) -> str | None:
    """O rótulo, se a mensagem é só ele e tem anexo; rótulo sem anexo é texto do cliente."""
    texto = mensagem.texto.strip()
    return texto if _ROTULO_DO_GATEWAY.fullmatch(texto) and mensagem.anexos else None


def _tem_texto(mensagem: Mensagem) -> bool:
    return bool(mensagem.texto.strip()) and _rotulo_do_gateway(mensagem) is None


def _so_figurinha(mensagens: list[Mensagem]) -> bool:
    """A rajada é só figurinha: não há o que ler nem o que conferir."""
    return all(_rotulo_do_gateway(mensagem) == _FIGURINHA for mensagem in mensagens)


def _so_imagem_ou_arquivo(mensagens: list[Mensagem]) -> bool:
    """Sem texto em nenhuma mensagem, sem áudio (que é transcrito) e com ao menos uma imagem ou um
    arquivo. A figurinha nunca conta como imagem, porque não é comprovante."""
    tipos = [
        anexo.get("tipo")
        for mensagem in mensagens
        if _rotulo_do_gateway(mensagem) != _FIGURINHA
        for anexo in mensagem.anexos
    ]
    if any(_tem_texto(mensagem) for mensagem in mensagens) or "audio" in tipos:
        return False
    return "image" in tipos or "file" in tipos


def _so_anexo_nao_aceito(mensagens: list[Mensagem], politica: Politica) -> bool:
    for mensagem in mensagens:
        if _tem_texto(mensagem):
            return False
        if any(anexo.get("tipo") in politica.anexos_aceitos for anexo in mensagem.anexos):
            return False
    return True


def _so_toque_no_botao_pix(mensagens: list[Mensagem]) -> bool:
    """A rajada é só o toque no botão. Com outro texto junto, o Jev lê tudo (pode ser dúvida)."""
    botao = BOTAO_PAGAR_PIX.texto.casefold()
    return all(mensagem.texto.strip().casefold() == botao for mensagem in mensagens)


def e_comando_de_reinicio(mensagem: Mensagem, politica: Politica) -> bool:
    return mensagem.texto.strip().casefold() == politica.comando_reinicio.casefold()


def humano_na_conversa(conversa: Conversa) -> bool:
    return conversa.human_owned or conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO


def avaliar_gates(
    conversa: Conversa,
    mensagens: list[Mensagem],
    agora: datetime,
    politica: Politica,
    sessao: Session,
    *,
    quitada_no_ctn: bool = False,
) -> Gate:
    """Os gates do turno, na ordem do módulo; o humano na conversa sempre barra.

    `quitada_no_ctn` (a conferência de hoje achou a cobrança quitada) troca o `Seguir` e o
    `PagarPix` por `Quitada`. Ela vem depois dos outros: humano, reinício, janela de atendimento,
    janela de 24h e teto valem primeiro, e a quitada nunca manda texto livre onde eles não deixam.
    """
    if not mensagens:
        return Ignorar("sem_mensagem")
    if conversa.cobranca.status is not StatusCobranca.ATIVA:
        return Ignorar("sem_cobranca_ativa")
    # O reinício vem antes do humano: o telefone da equipe recomeça a conversa de teste mesmo
    # depois de um handoff, sem precisar devolvê-la ao robô no Chatwoot.
    # A equipe casa com ou sem o 9 do celular (A1).
    da_equipe = not variantes_telefone(conversa.cobranca.cliente.telefone).isdisjoint(
        politica.telefones_equipe
    )
    if da_equipe and any(e_comando_de_reinicio(mensagem, politica) for mensagem in mensagens):
        return Reiniciar()
    if humano_na_conversa(conversa):
        return Ignorar("humano_na_conversa")
    if _so_anexo_nao_aceito(mensagens, politica):
        return Ignorar("anexo_nao_aceito")
    if _so_figurinha(mensagens):
        return Ignorar("figurinha")
    toque = _so_toque_no_botao_pix(mensagens)
    if not toque and not dentro_da_janela(agora, politica):
        # O toque no botão é a exceção à D16 (D40): quem toca quer pagar agora, e a resposta é uma
        # mensagem só. Os outros gates (humano, 24h, teto) continuam valendo.
        return Adiar(proxima_abertura(agora, politica))
    if agora - max(mensagem.momento for mensagem in mensagens) > JANELA_WHATSAPP:
        return ForaDas24h()
    if mensagens_no_teto(sessao, conversa.id) >= politica.teto_resolucao:
        # Quem já saiu pelo teto e foi devolvido ao robô também chega aqui: o orquestrador decide
        # entre a 6ª mensagem e o retorno silencioso ao humano.
        return Fixa(TipoFixa.CANAL_EXTERNO)
    if quitada_no_ctn:
        return Quitada()
    if _so_imagem_ou_arquivo(mensagens):
        return ComprovanteSemTexto()
    if toque:
        return PagarPix()
    return Seguir()


def _resposta_ao_toque(sessao: Session, mensagem: Mensagem) -> bool:
    """A mensagem do agente responde só ao toque no botão (D40): a rajada que ela responde."""
    anteriores = sessao.scalars(
        select(Mensagem)
        .where(Mensagem.conversa_id == mensagem.conversa_id, Mensagem.id < mensagem.id)
        .order_by(Mensagem.id.desc())
    )
    rajada = []
    for anterior in anteriores:
        if anterior.origem is not OrigemMensagem.CLIENTE:
            break
        rajada.append(anterior)
    return bool(rajada) and _so_toque_no_botao_pix(rajada)


def avaliar_envio(
    conversa: Conversa, mensagem: Mensagem, agora: datetime, politica: Politica, sessao: Session
) -> Gate:
    """Os gates da mensagem do agente já gravada (PENDENTE), antes de ela ir ao Chatwoot.

    O que pode ter mudado na espera: cobrança, humano, janela de atendimento, janela de 24h e teto.
    Só `Seguir`, `Ignorar` (o texto não sai mais), `Adiar` (sai quando a janela abrir) e
    `ForaDas24h` (o texto não sai mais e o humano assume, como no turno normal) voltam.
    """
    if conversa.cobranca.status is not StatusCobranca.ATIVA:
        return Ignorar("sem_cobranca_ativa")
    if humano_na_conversa(conversa):
        return Ignorar("humano_na_conversa")
    if not dentro_da_janela(agora, politica) and not _resposta_ao_toque(sessao, mensagem):
        return Adiar(proxima_abertura(agora, politica))
    ultima = sessao.scalar(
        select(func.max(Mensagem.momento)).where(
            Mensagem.conversa_id == conversa.id, Mensagem.origem == OrigemMensagem.CLIENTE
        )
    )
    if ultima is not None and agora - ultima > JANELA_WHATSAPP:
        return ForaDas24h()
    # A própria mensagem já conta no teto: o que a barra são as outras.
    outras = mensagens_no_teto(sessao, conversa.id) - (1 if mensagem.conta_no_teto else 0)
    if outras >= politica.teto_resolucao:
        return Ignorar("teto_encerrado")
    return Seguir()
