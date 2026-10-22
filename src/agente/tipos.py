"""Tipos de valor compartilhados pelo banco e pela lógica do turno."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Laudo:
    """A resposta do Jev a uma pergunta tipada: sim/não, nota ou escolha."""

    pergunta: str
    valor: bool | float | str
    probabilidade: float
    confianca: float


class EstadoConversa(StrEnum):
    """Estados da conversa (arquitetura, seção 3)."""

    INICIADO = "INICIADO"
    ENGAJADO = "ENGAJADO"
    EM_NEGOCIACAO = "EM_NEGOCIACAO"
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    PAGO = "PAGO"
    ENCERRADO = "ENCERRADO"
    EM_RISCO = "EM_RISCO"
    ATENDIMENTO_HUMANO = "ATENDIMENTO_HUMANO"


class MotivoHumano(StrEnum):
    """Por que a conversa saiu do robô. Vira a etiqueta no Chatwoot."""

    RETENCAO = "RETENCAO"
    JURIDICO = "JURIDICO"
    CONCILIACAO = "CONCILIACAO"
    CONTESTACAO = "CONTESTACAO"
    CAUTELA = "CAUTELA"
    ATRITO = "ATRITO"
    ENERGISA = "ENERGISA"
    TOM = "TOM"
    BAIXA_CONFIANCA = "BAIXA_CONFIANCA"
    JEV_INDISPONIVEL = "JEV_INDISPONIVEL"
    VALIDACAO = "VALIDACAO"
    OBJECAO_SEM_DESCONTO = "OBJECAO_SEM_DESCONTO"
    BOLETO = "BOLETO"
    TETO = "TETO"
    DECISAO_JEV2 = "DECISAO_JEV2"


class FormaPagamento(StrEnum):
    PIX = "PIX"
    LINK_CTN = "LINK_CTN"


class OrigemMensagem(StrEnum):
    CLIENTE = "CLIENTE"
    AGENTE = "AGENTE"
    FIXA = "FIXA"
    HUMANO = "HUMANO"
    ABERTURA = "ABERTURA"


class StatusCobranca(StrEnum):
    ATIVA = "ATIVA"
    PAGA = "PAGA"
    ENCERRADA = "ENCERRADA"


class StatusPagamento(StrEnum):
    PENDENTE = "PENDENTE"
    PAGO = "PAGO"
    EXPIRADO = "EXPIRADO"


class Modo(StrEnum):
    SOMBRA = "SOMBRA"
    ATIVO = "ATIVO"


class BatchJev(StrEnum):
    JEV1 = "JEV1"
    JEV2 = "JEV2"
    JEV3 = "JEV3"


class ResultadoTurno(StrEnum):
    ENVIADO = "ENVIADO"
    FIXA = "FIXA"
    HANDOFF = "HANDOFF"
    ADIADO = "ADIADO"
    IGNORADO = "IGNORADO"
    SOMBRA = "SOMBRA"
    ERRO = "ERRO"
