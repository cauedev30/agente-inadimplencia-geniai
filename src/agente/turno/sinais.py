"""Os sinais do turno de uma chamada ao Luna: descrições, gatilhos, leitura e avaliação.

O LLM devolve os sinais da mensagem do cliente (tom, retenção, jurídico, ...) num JSON só. Aqui
moram os nomes que o turno usa para descrevê-los: a descrição de cada sinal, os gatilhos que
levam ao humano, a leitura em laudos e o rótulo que o turno grava como ação principal.
"""

from dataclasses import dataclass

from agente.tipos import Laudo, MotivoHumano
from agente.turno.acoes import Acao

# O que cada sinal quer dizer, como o prompt o descreve ao Luna.
SINAIS_DO_PROMPT: dict[str, str] = {
    "quer_retencao": "quer cancelar, encerrar a relação ou ameaça sair",
    "risco_juridico": "cita Procon, advogado, processo, ação",
    "alega_pagamento": "afirma que já pagou",
    "contesta_divida": (
        "afirma que não deve essa dívida, que não é a pessoa cobrada ou que não reconhece o "
        "contrato (nunca assinei, o valor não é meu); só recusar pagar ou reclamar da clínica não "
        "é contestar, mas recusar e contestar na mesma frase conta"
    ),
    "desconfia_golpe": (
        "desconfia que a cobrança é golpe ou pede prova de que a mensagem é mesmo do "
        "Cartão de Todos"
    ),
    "vulnerabilidade": "desemprego, doença, luto, dificuldade",
    "atrito_clinica": "reclama da clínica ou do atendimento da rede",
    "quer_conta_energia": "quer pagar na conta de energia (Energisa)",
    "quer_humano": (
        "pede para falar com uma pessoa, um atendente ou a equipe, ou recusa falar com um robô"
    ),
    "terceiro": "quem escreve não é o devedor",
}

# Os gatilhos de saída, na ordem em que vencem; o tom vem depois, na decisão do turno.
GATILHOS = {
    MotivoHumano.RETENCAO: "quer_retencao",
    MotivoHumano.JURIDICO: "risco_juridico",
    MotivoHumano.CONCILIACAO: "alega_pagamento",
    MotivoHumano.CONTESTACAO: "contesta_divida",
    MotivoHumano.DESCONFIANCA: "desconfia_golpe",
    MotivoHumano.CAUTELA: "vulnerabilidade",
    MotivoHumano.ATRITO: "atrito_clinica",
    MotivoHumano.ENERGISA: "quer_conta_energia",
}


@dataclass(frozen=True)
class LeituraJev1:
    laudos: tuple[Laudo, ...]


# Quando cada ação se aplica. Um dict por ação, para a ordem dos critérios nunca desalinhar do enum.
CRITERIOS_DA_ACAO = {
    Acao.INFORM_DEBT: (
        "o cliente pergunta quanto deve, não entendeu o valor ou pede que o expliquem, pergunta "
        "como pagar, pede parcelamento (a dívida é à vista) ou diz que quer pagar, sem escolher "
        "Pix nem cartão; o sistema junta a forma de pagamento"
    ),
    Acao.OFFER_DISCOUNT: (
        "o cliente acha caro, diz que não consegue pagar tudo, pede desconto ou um valor melhor, "
        "mesmo que já tenha pedido antes; se já recebeu uma oferta, só quando a recusa ou pede "
        "menos; o sistema decide se há desconto"
    ),
    Acao.SEND_CTN_LINK: (
        "o cliente pede o link, quer pagar no cartão de crédito ou aceita a oferta com desconto "
        "já enviada (o desconto só vale pelo link), mesmo com condição como 'se tiver desconto eu "
        "pago'"
    ),
    Acao.GENERATE_PIX: "o cliente pede o Pix pelo nome (código, chave, pagar no Pix)",
    Acao.OUT_OF_SCOPE: (
        "o assunto não é a dívida nem o pagamento (horário, endereço, outro serviço)"
    ),
    Acao.NONE: (
        "o cliente só cumprimenta (bom dia, oi, olá), confirma que leu ou agradece, sem pergunta "
        "nem pedido"
    ),
}

# O que cada ação pede ao texto. O nome da ação é o da arquitetura (seção 4).
DESCRICAO_ACOES = {
    "INFORM_DEBT": "apresentar o valor em aberto, sem desconto",
    "OFFER_DISCOUNT": "oferecer o valor com desconto, que só vale no link do cartão",
    "SEND_CTN_LINK": "mandar o link em que o cliente paga e já cadastra o cartão",
    "GENERATE_PIX": (
        "mandar o Pix copia e cola do valor integral; se o histórico já tem uma oferta com "
        "desconto, dizer que no Pix vale o valor integral e que o desconto só vale pelo link"
    ),
    "OUT_OF_SCOPE": (
        "dizer, com gentileza e numa frase completa, que esse assunto não é tratado por aqui e que "
        "esta conversa é sobre a mensalidade em aberto, sem afirmar nada sobre o assunto"
    ),
}

# O prefixo da ação principal do turno cujo texto não saiu: nada do que ela fez ficou valendo.
NAO_EXECUTADA = "NAO_EXECUTADA:"


def rotulo_humano(motivo: MotivoHumano) -> str:
    """O rótulo da saída para o humano: `HUMAN:<motivo>`."""
    return f"HUMAN:{motivo.value}"


@dataclass(frozen=True)
class Avaliacao:
    """A ação do turno e os anexos, ou o motivo de ir para o humano."""

    acao: Acao | None
    anexos: list[Acao]
    motivo: MotivoHumano | None
    erro: str | None = None

    @property
    def rotulo(self) -> str:
        """O que o turno grava como ação principal: a ação ou `HUMAN:<motivo>`."""
        return self.acao.value if self.acao is not None else rotulo_humano(self.motivo)

    @property
    def rotulo_nao_executado(self) -> str:
        """A ação decidida cujo texto não saiu: o backend desfez o que ela tinha feito."""
        return f"{NAO_EXECUTADA}{self.rotulo}"
