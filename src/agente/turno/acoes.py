"""As regras de ação do Jev 2: limiares, pré-condições e o que vai junto (arquitetura, seção 4).

Nenhuma ação sai sem passar pelas três, nesta ordem. O Jev escolhe a ação principal; o código
autoriza e anexa as outras pela tabela fixa da D37.
"""

from dataclasses import dataclass
from enum import StrEnum

from agente.db.modelos import Cobranca, Conversa
from agente.dominio.desconto import proxima_oferta
from agente.politica import Politica
from agente.tipos import Laudo, MotivoHumano
from agente.turno.jev1 import LeituraJev1


class Acao(StrEnum):
    INFORM_DEBT = "INFORM_DEBT"
    OFFER_DISCOUNT = "OFFER_DISCOUNT"
    SEND_CTN_LINK = "SEND_CTN_LINK"
    GENERATE_PIX = "GENERATE_PIX"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NONE = "NONE"
    HUMAN = "HUMAN"


@dataclass(frozen=True)
class Decisao:
    acao: Acao
    probabilidade: float
    confianca: float

    @property
    def laudo(self) -> Laudo:
        return Laudo("acao", self.acao.value, self.probabilidade, self.confianca)


def aplicar_limiares(decisao: Decisao, politica: Politica) -> Acao | MotivoHumano:
    """Abaixo da confiança mínima ou do limiar da ação, o robô não chuta: vai para o humano."""
    if decisao.confianca < politica.confianca_minima:
        return MotivoHumano.BAIXA_CONFIANCA
    if decisao.acao is Acao.HUMAN:
        return MotivoHumano.DECISAO_JEV2
    if decisao.probabilidade < politica.limiares_jev2[decisao.acao.value]:
        return MotivoHumano.BAIXA_CONFIANCA
    return decisao.acao


def aplicar_precondicoes(
    acao: Acao,
    conversa: Conversa,
    cobranca: Cobranca,
    leitura: LeituraJev1,
    politica: Politica,
) -> Acao | MotivoHumano:
    """O desconto só sai com objeção, na régua com desconto e com um degrau seguinte.

    Na régua sem desconto, a objeção recebe o débito com a explicação da condição, e a que chega
    ao limite da política (`objecoes_sem_desconto_ate_humano`) vai para o humano. Na régua com
    desconto, a objeção depois do último degrau também vai (D38), seja qual for a ação escolhida.
    """
    objecao = leitura.sim("objecao_valor")
    if objecao and politica.reguas[cobranca.regua].desconto_max_centavos == 0:
        if conversa.objecoes_sem_desconto + 1 >= politica.objecoes_sem_desconto_ate_humano:
            return MotivoHumano.OBJECAO_SEM_DESCONTO
        return Acao.INFORM_DEBT if acao is Acao.OFFER_DISCOUNT else acao
    oferta = proxima_oferta(
        cobranca.regua, cobranca.divida_centavos, conversa.degrau_atual, politica
    )
    if acao is Acao.OFFER_DISCOUNT and not objecao:
        # O Jev 2 quis o desconto sem o Jev 1 ver objeção: o robô não chuta.
        return MotivoHumano.BAIXA_CONFIANCA
    if objecao and oferta is None:
        return MotivoHumano.OBJECAO_SEM_DESCONTO
    return acao


def anexar(acao: Acao, cobranca: Cobranca) -> list[Acao]:
    """A tabela fixa da D37: o que vai junto da ação principal na mesma mensagem."""
    if acao is Acao.INFORM_DEBT:
        return [Acao.SEND_CTN_LINK if cobranca.propenso_cartao else Acao.GENERATE_PIX]
    if acao is Acao.OFFER_DISCOUNT:
        return [Acao.SEND_CTN_LINK]
    return []


def autorizar(
    decisao: Decisao,
    conversa: Conversa,
    cobranca: Cobranca,
    leitura: LeituraJev1,
    politica: Politica,
) -> tuple[Acao | MotivoHumano, list[Acao]]:
    """Limiares, pré-condições e anexos, nesta ordem. Para o humano, não vai nada junto."""
    acao = aplicar_limiares(decisao, politica)
    if isinstance(acao, Acao):
        acao = aplicar_precondicoes(acao, conversa, cobranca, leitura, politica)
    if isinstance(acao, MotivoHumano):
        return acao, []
    return acao, anexar(acao, cobranca)
