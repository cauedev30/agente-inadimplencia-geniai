"""Gates em código (arquitetura, seção 1): certezas que ficam fora do Jev.

O segredo, o JSON, a conta, a inbox e o dedupe já ficaram no webhook (tarefa 3.4). Aqui ficam os
gates do turno, nesta ordem: cobrança ativa, humano na conversa, reinício, anexo, janela de
atendimento, janela de 24h do WhatsApp, teto e o toque no botão "Pagar com Pix". O toque no botão
é a única mensagem que dispensa a janela de atendimento (D40).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import mensagens_no_teto
from agente.db.modelos import Conversa, Mensagem
from agente.dominio.janela import dentro_da_janela, proxima_abertura
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
class Retomar:
    """Passou das 24h do WhatsApp: só um template pode sair (C3)."""


@dataclass(frozen=True)
class Fixa:
    tipo: TipoFixa


@dataclass(frozen=True)
class PagarPix:
    """O cliente tocou no botão "Pagar com Pix" (D26): vai direto para o GENERATE_PIX, sem o Jev."""


@dataclass(frozen=True)
class Reiniciar:
    """Comando de reinício, só para os telefones da equipe."""


Gate = Seguir | Ignorar | Adiar | Retomar | Fixa | Reiniciar | PagarPix


def _so_anexo_nao_aceito(mensagens: list[Mensagem], politica: Politica) -> bool:
    for mensagem in mensagens:
        if mensagem.texto.strip():
            return False
        if any(anexo.get("tipo") in politica.anexos_aceitos for anexo in mensagem.anexos):
            return False
    return True


def _so_toque_no_botao_pix(mensagens: list[Mensagem]) -> bool:
    """A rajada é só o toque no botão. Com outro texto junto, o Jev lê tudo (pode ser dúvida)."""
    botao = BOTAO_PAGAR_PIX.texto.casefold()
    return all(mensagem.texto.strip().casefold() == botao for mensagem in mensagens)


def _ja_saiu_fixa(sessao: Session, conversa_id: int) -> bool:
    consulta = select(
        exists().where(Mensagem.conversa_id == conversa_id, Mensagem.origem == OrigemMensagem.FIXA)
    )
    return bool(sessao.scalar(consulta))


def humano_na_conversa(conversa: Conversa) -> bool:
    return conversa.human_owned or conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO


def avaliar_gates(
    conversa: Conversa,
    mensagens: list[Mensagem],
    agora: datetime,
    politica: Politica,
    sessao: Session,
    *,
    barrar_humano: bool = True,
) -> Gate:
    """Em sombra, o humano na conversa não barra: a sombra lê enquanto os humanos atendem (T7)."""
    if not mensagens:
        return Ignorar("sem_mensagem")
    if conversa.cobranca.status is not StatusCobranca.ATIVA:
        return Ignorar("sem_cobranca_ativa")
    if barrar_humano and humano_na_conversa(conversa):
        return Ignorar("humano_na_conversa")
    textos = [mensagem.texto.strip() for mensagem in mensagens]
    if (
        politica.comando_reinicio in textos
        and conversa.cobranca.cliente.telefone in politica.telefones_equipe
    ):
        return Reiniciar()
    if _so_anexo_nao_aceito(mensagens, politica):
        return Ignorar("anexo_nao_aceito")
    toque = _so_toque_no_botao_pix(mensagens)
    if not toque and not dentro_da_janela(agora, politica):
        # O toque no botão é a exceção à D16 (D40): quem toca quer pagar agora, e a resposta é uma
        # mensagem só. Os outros gates (humano, 24h, teto) continuam valendo.
        return Adiar(proxima_abertura(agora, politica))
    if agora - max(mensagem.momento for mensagem in mensagens) > JANELA_WHATSAPP:
        return Retomar()
    if mensagens_no_teto(sessao, conversa.id) >= politica.teto_resolucao:
        if _ja_saiu_fixa(sessao, conversa.id):
            return Ignorar("teto_encerrado")
        return Fixa(TipoFixa.CANAL_EXTERNO)
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
    Só `Seguir`, `Ignorar` (o texto não sai mais) e `Adiar` (sai quando a janela abrir) voltam.
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
        return Ignorar("fora_das_24h")
    # A própria mensagem já conta no teto: o que a barra são as outras.
    outras = mensagens_no_teto(sessao, conversa.id) - (1 if mensagem.conta_no_teto else 0)
    if outras >= politica.teto_resolucao:
        return Ignorar("teto_encerrado")
    return Seguir()
