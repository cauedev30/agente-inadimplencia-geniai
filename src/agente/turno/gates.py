"""Gates em código (arquitetura, seção 1): certezas que ficam fora do Jev.

O segredo, o JSON, a conta, a inbox e o dedupe já ficaram no webhook (tarefa 3.4). Aqui ficam os
gates do turno, nesta ordem: cobrança ativa, humano na conversa, reinício, anexo, janela de
atendimento, janela de 24h do WhatsApp e teto.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

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
class Reiniciar:
    """Comando de reinício, só para os telefones da equipe."""


Gate = Seguir | Ignorar | Adiar | Retomar | Fixa | Reiniciar


def _so_anexo_nao_aceito(mensagens: list[Mensagem], politica: Politica) -> bool:
    for mensagem in mensagens:
        if mensagem.texto.strip():
            return False
        if any(anexo.get("tipo") in politica.anexos_aceitos for anexo in mensagem.anexos):
            return False
    return True


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
    if not dentro_da_janela(agora, politica):
        return Adiar(proxima_abertura(agora, politica))
    if agora - max(mensagem.momento for mensagem in mensagens) > JANELA_WHATSAPP:
        return Retomar()
    if mensagens_no_teto(sessao, conversa.id) >= politica.teto_resolucao:
        if _ja_saiu_fixa(sessao, conversa.id):
            return Ignorar("teto_encerrado")
        return Fixa(TipoFixa.CANAL_EXTERNO)
    return Seguir()
