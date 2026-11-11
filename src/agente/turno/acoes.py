"""As ações do turno e o que vai junto de cada uma (arquitetura, seção 4).

O LLM escolhe a ação principal; o código anexa as outras pela tabela fixa da D37.
"""

from enum import StrEnum

from agente.db.modelos import Cobranca


class Acao(StrEnum):
    INFORM_DEBT = "INFORM_DEBT"
    OFFER_DISCOUNT = "OFFER_DISCOUNT"
    SEND_CTN_LINK = "SEND_CTN_LINK"
    GENERATE_PIX = "GENERATE_PIX"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NONE = "NONE"
    HUMAN = "HUMAN"


def anexar(acao: Acao, cobranca: Cobranca) -> list[Acao]:
    """A tabela fixa da D37: o que vai junto da ação principal na mesma mensagem."""
    if acao is Acao.INFORM_DEBT:
        return [Acao.SEND_CTN_LINK if cobranca.propenso_cartao else Acao.GENERATE_PIX]
    if acao is Acao.OFFER_DISCOUNT:
        return [Acao.SEND_CTN_LINK]
    return []
