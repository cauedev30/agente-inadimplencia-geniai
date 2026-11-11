"""Os sinais do turno de uma chamada ao Luna: perguntas, gatilhos, leitura e avaliação (D47).

O LLM devolve os sinais da mensagem do cliente (tom, retenção, jurídico, ...) num JSON só. Aqui
moram os nomes que o turno usa para descrevê-los: a descrição de cada sinal, os gatilhos que
levam ao humano, a leitura em laudos e o rótulo que o turno grava como ação principal.
"""

from dataclasses import dataclass
from typing import Literal

from agente.db.modelos import Cobranca, Conversa
from agente.dominio.desconto import proxima_oferta, valor_com_desconto
from agente.politica import Politica
from agente.tipos import Laudo, MotivoHumano
from agente.turno.acoes import Acao

TipoPergunta = Literal["bool", "score", "escolha"]


@dataclass(frozen=True)
class Pergunta:
    """Uma pergunta tipada. A nota (score) e a escolha respondem com uma das opções.

    `criterios` (opcional) descreve quando cada opção se aplica, na mesma ordem de `opcoes`.
    """

    nome: str
    tipo: TipoPergunta
    descricao: str
    opcoes: tuple[str, ...] | None = None
    criterios: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.tipo == "bool" and self.opcoes is not None:
            raise ValueError(f"a pergunta {self.nome} é sim/não e não leva opções")
        if self.tipo != "bool" and not self.opcoes:
            raise ValueError(f"a pergunta {self.nome} ({self.tipo}) precisa das opções")
        if self.criterios is not None:
            if self.tipo == "bool":
                raise ValueError(f"a pergunta {self.nome} é sim/não e não leva critérios")
            if len(self.criterios) != len(self.opcoes or ()):
                raise ValueError(
                    f"a pergunta {self.nome} precisa de um critério por opção "
                    f"({len(self.opcoes or ())} opções, {len(self.criterios)} critérios)"
                )

    def aceita(self, valor) -> bool:
        """O valor tem o tipo da pergunta: sim/não de verdade, ou uma das opções."""
        if self.tipo == "bool":
            return isinstance(valor, bool)
        return valor in (self.opcoes or ())


TONS = ("cordial", "neutro", "impaciente", "irritado", "hostil")
INTENCOES = ("nenhuma", "hesitante", "disposto", "pronto")

# Quando cada nível se aplica. Um dict por opção, para a ordem nunca desalinhar de TONS e INTENCOES.
CRITERIOS_DO_TOM = {
    "cordial": "gentileza explícita: cumprimenta com bom dia ou boa tarde, agradece, diz por favor",
    "neutro": "direto e sem emoção: pergunta ou responde o necessário (um oi sozinho é neutro)",
    "impaciente": "pressa ou incômodo leve: insiste, cobra resposta, reclama da demora",
    "irritado": "raiva ou aspereza: reclama com raiva, recusa de forma ríspida",
    "hostil": "agressivo: xinga, ofende ou ameaça",
}
CRITERIOS_DA_INTENCAO = {
    "nenhuma": "recusa pagar ou não fala de pagamento",
    "hesitante": "considera pagar, mas põe obstáculo ou só pergunta o valor sem se comprometer",
    "disposto": "quer pagar e pergunta por uma forma possível",
    "pronto": "pede o meio de pagar agora (como pago, link, Pix)",
}

PERGUNTAS_JEV1 = (
    Pergunta(
        "tom",
        "score",
        "o tom do cliente",
        TONS,
        tuple(CRITERIOS_DO_TOM[tom] for tom in TONS),
    ),
    Pergunta("quer_retencao", "bool", "quer cancelar, encerrar a relação ou ameaça sair"),
    Pergunta("risco_juridico", "bool", "cita Procon, advogado, processo, ação"),
    Pergunta("alega_pagamento", "bool", "afirma que já pagou"),
    Pergunta(
        "contesta_divida",
        "bool",
        "afirma que não deve essa dívida, que não é a pessoa cobrada ou que não reconhece o "
        "contrato (nunca assinei, o valor não é meu); só recusar pagar ou reclamar da clínica não "
        "é contestar, mas recusar e contestar na mesma frase conta",
    ),
    Pergunta(
        "desconfia_golpe",
        "bool",
        "desconfia que a cobrança é golpe ou pede prova de que a mensagem é mesmo do "
        "Cartão de Todos",
    ),
    Pergunta("terceiro", "bool", "quem escreve não é o devedor"),
    Pergunta("vulnerabilidade", "bool", "desemprego, doença, luto, dificuldade"),
    Pergunta(
        "objecao_valor", "bool", "acha caro, diz que não consegue pagar tudo ou pede desconto"
    ),
    Pergunta("atrito_clinica", "bool", "reclama da clínica ou do atendimento da rede"),
    Pergunta("quer_conta_energia", "bool", "quer pagar na conta de energia (Energisa)"),
    Pergunta(
        "intencao_pagar",
        "score",
        "a intenção de pagar",
        INTENCOES,
        tuple(CRITERIOS_DA_INTENCAO[intencao] for intencao in INTENCOES),
    ),
    Pergunta("entendeu_proposta", "bool", "entendeu o que foi oferecido"),
    Pergunta("repete_sem_avancar", "bool", "está em loop, repetindo o mesmo ponto"),
)

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

    def laudo(self, pergunta: str) -> Laudo:
        for laudo in self.laudos:
            if laudo.pergunta == pergunta:
                return laudo
        raise KeyError(pergunta)

    def sim(self, pergunta: str) -> bool:
        return self.laudo(pergunta).valor is True

    @property
    def tom(self) -> str:
        return str(self.laudo("tom").valor)


# O fim das ações amplas: o assunto sensível vence o pedido comum (revisão Opus, achado 2).
_SENSIVEL = "; se a mensagem também traz um assunto sensível, vale HUMAN"

# Quando cada ação se aplica. Um dict por ação, para a ordem dos critérios nunca desalinhar do enum.
CRITERIOS_DA_ACAO = {
    Acao.INFORM_DEBT: (
        "o cliente pergunta quanto deve, não entendeu o valor ou pede que o expliquem, pergunta "
        "como pagar, pede parcelamento (a dívida é à vista) ou diz que quer pagar, sem escolher "
        "Pix nem cartão; o sistema junta a forma de pagamento" + _SENSIVEL
    ),
    Acao.OFFER_DISCOUNT: (
        "o cliente acha caro, diz que não consegue pagar tudo, pede desconto ou um valor melhor, "
        "mesmo que já tenha pedido antes; se já recebeu uma oferta, só quando a recusa ou pede "
        "menos; o sistema decide se há desconto" + _SENSIVEL
    ),
    Acao.SEND_CTN_LINK: (
        "o cliente pede o link, quer pagar no cartão de crédito ou aceita a oferta com desconto "
        "já enviada (o desconto só vale pelo link), mesmo com condição como 'se tiver desconto eu "
        "pago'"
    ),
    Acao.GENERATE_PIX: "o cliente pede o Pix pelo nome (código, chave, pagar no Pix)",
    Acao.OUT_OF_SCOPE: (
        "o assunto não é a dívida nem o pagamento (horário, endereço, outro serviço)" + _SENSIVEL
    ),
    Acao.NONE: (
        "o cliente só cumprimenta (bom dia, oi, olá), confirma que leu ou agradece, sem pergunta "
        "nem pedido"
    ),
    Acao.HUMAN: (
        "o cliente pede uma pessoa, ou a mensagem traz um assunto sensível mesmo junto de outro "
        "pedido: desconfia de golpe, quer cancelar, cita Procon ou advogado, diz que já pagou, diz "
        "que não deve ou que não é o devedor, fala de desemprego, doença ou luto; na dúvida entre "
        "esta e outra, escolha esta"
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


def numeros_previstos(
    avaliacao: Avaliacao, conversa: Conversa, cobranca: Cobranca, politica: Politica
) -> dict:
    """A prévia da sombra: os números que a ação teria levado, sem gerar link nem Pix.

    No modo ativo, quem gera os números de verdade é o `executar`.
    """
    if avaliacao.acao is None:
        return {}
    acoes = [avaliacao.acao, *avaliacao.anexos]
    numeros: dict = {"divida_centavos": cobranca.divida_centavos}
    degrau = conversa.degrau_atual
    if Acao.OFFER_DISCOUNT in acoes:
        oferta = proxima_oferta(cobranca.regua, cobranca.divida_centavos, degrau, politica)
        numeros |= {
            "degrau": oferta.degrau,
            "desconto_centavos": oferta.desconto_centavos,
            "valor_final_centavos": oferta.valor_final_centavos,
        }
        degrau = oferta.degrau
    if Acao.SEND_CTN_LINK in acoes:
        numeros["valor_link_centavos"] = valor_com_desconto(
            cobranca.regua, cobranca.divida_centavos, degrau, politica
        )
    if Acao.GENERATE_PIX in acoes:
        # O Pix paga sempre o integral (D34).
        numeros["valor_pix_centavos"] = cobranca.divida_centavos
    return numeros
