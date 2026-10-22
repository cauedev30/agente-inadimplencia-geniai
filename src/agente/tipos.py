"""Tipos de valor compartilhados pelo banco e pela lógica do turno."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True)
class Botao:
    """Um botão do WhatsApp: de resposta (volta como mensagem) ou de link."""

    tipo: Literal["resposta", "url"]
    texto: str
    valor: str


class FormaHistorico(StrEnum):
    """Como o cliente pagou uma mensalidade, segundo o histórico do CTN."""

    PIX = "PIX"
    CARTAO = "CARTAO"
    BOLETO = "BOLETO"
    ENERGIA = "ENERGIA"
    OUTRO = "OUTRO"


@dataclass(frozen=True)
class PagamentoHistorico:
    data: date
    forma: FormaHistorico
    valor_centavos: int


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
    # EM_RISCO sem melhora no turno de recuperação (arquitetura, seção 3).
    SEM_MELHORA = "SEM_MELHORA"
    BAIXA_CONFIANCA = "BAIXA_CONFIANCA"
    JEV_INDISPONIVEL = "JEV_INDISPONIVEL"
    REDATOR_INDISPONIVEL = "REDATOR_INDISPONIVEL"
    # O CTN, o Pix ou o Chatwoot falhou em todas as tentativas do turno (arquitetura, seção 9).
    INTEGRACAO_INDISPONIVEL = "INTEGRACAO_INDISPONIVEL"
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


class EstadoEnvio(StrEnum):
    """Onde está a mensagem do agente: gravada, confirmada no Chatwoot ou descartada."""

    PENDENTE = "PENDENTE"
    ENVIADA = "ENVIADA"
    DESCARTADA = "DESCARTADA"


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
    # O Jev 2 decidiu não agir (NONE): o turno roda e nada sai.
    SEM_ENVIO = "SEM_ENVIO"
    ERRO = "ERRO"
    # O texto foi gravado para sair e nunca saiu (o humano assumiu, as tentativas acabaram).
    DESCARTADO = "DESCARTADO"


class TipoFixa(StrEnum):
    """As mensagens fixas, sem LLM nem Jev (D33, D19)."""

    CONFIRMACAO = "CONFIRMACAO"
    CONFIRMACAO_CONVITE_CARTAO = "CONFIRMACAO_CONVITE_CARTAO"
    CANAL_EXTERNO = "CANAL_EXTERNO"
