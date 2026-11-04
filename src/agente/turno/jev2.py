"""Jev 2: a decisão da ação principal do turno (arquitetura, seção 4).

O Jev só responde perguntas tipadas, então a decisão é uma escolha entre as ações; o que vai junto é
anexado pelo código (D37). Nada de conta para o Jev: o degrau e a propensão vêm do backend.
"""

from dataclasses import replace

from agente.integracoes.jev import EstadoJev, Jev, Pergunta
from agente.turno.acoes import Acao, Decisao
from agente.turno.jev1 import LeituraJev1

# O fim das ações amplas: o assunto sensível vence o pedido comum (revisão Opus, achado 2).
_SENSIVEL = "; se a mensagem também traz um assunto sensível, vale HUMAN"

# Quando cada ação se aplica. Um dict por ação, para a ordem dos critérios nunca desalinhar do enum.
CRITERIOS_DA_ACAO = {
    Acao.INFORM_DEBT: (
        "o cliente pergunta quanto deve, como pagar ou diz que quer pagar, sem escolher Pix nem "
        "cartão; o sistema junta a forma de pagamento" + _SENSIVEL
    ),
    Acao.OFFER_DISCOUNT: (
        "o cliente acha caro, diz que não consegue pagar tudo, pede desconto ou um valor melhor, "
        "mesmo que já tenha pedido antes; o sistema decide se há desconto" + _SENSIVEL
    ),
    Acao.SEND_CTN_LINK: "o cliente pede o link ou quer pagar no cartão de crédito",
    Acao.GENERATE_PIX: "o cliente pede o Pix pelo nome (código, chave, pagar no Pix)",
    Acao.OUT_OF_SCOPE: (
        "o assunto não é a dívida nem o pagamento (horário, endereço, outro serviço)" + _SENSIVEL
    ),
    Acao.NONE: "o cliente só confirma que leu ou agradece, sem pergunta nem pedido",
    Acao.HUMAN: (
        "o cliente pede uma pessoa, ou a mensagem traz um assunto sensível mesmo junto de outro "
        "pedido: desconfia de golpe, quer cancelar, cita Procon ou advogado, diz que já pagou, diz "
        "que não deve ou que não é o devedor, fala de desemprego, doença ou luto; na dúvida entre "
        "esta e outra, escolha esta"
    ),
}

PERGUNTAS_JEV2 = (
    Pergunta(
        "acao",
        "escolha",
        "a ação principal da próxima resposta do agente de cobrança à última mensagem do cliente",
        tuple(acao.value for acao in Acao),
        tuple(CRITERIOS_DA_ACAO[acao] for acao in Acao),
    ),
)


def decidir(jev: Jev, estado: EstadoJev, leitura: LeituraJev1) -> Decisao:
    [laudo] = jev.avaliar(PERGUNTAS_JEV2, replace(estado, leitura=leitura.laudos))
    return Decisao(Acao(laudo.valor), laudo.probabilidade, laudo.confianca)
