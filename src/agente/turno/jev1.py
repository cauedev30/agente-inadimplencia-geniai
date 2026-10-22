"""Jev 1: a leitura de toda mensagem do cliente e os gatilhos de saída (arquitetura, seção 2).

As 13 perguntas rodam num batch só. Um gatilho de saída leva a conversa para o humano; o Jev só
classifica, e quem decide a saída é a regra daqui.
"""

from dataclasses import dataclass

from agente.integracoes.jev import EstadoJev, Jev, Pergunta
from agente.politica import Politica
from agente.tipos import Laudo, MotivoHumano

TONS = ("cordial", "neutro", "impaciente", "irritado", "hostil")
TONS_DE_SAIDA = frozenset({"irritado", "hostil"})
# O tom que azedou sem chegar à saída: a conversa fica EM_RISCO (seção 3).
TONS_DE_RISCO = frozenset({"impaciente"})
INTENCOES = ("nenhuma", "hesitante", "disposto", "pronto")

PERGUNTAS_JEV1 = (
    Pergunta("tom", "score", "o tom do cliente", TONS),
    Pergunta("quer_retencao", "bool", "quer cancelar, encerrar a relação ou ameaça sair"),
    Pergunta("risco_juridico", "bool", "cita Procon, advogado, processo, ação"),
    Pergunta("alega_pagamento", "bool", "afirma que já pagou"),
    Pergunta("contesta_divida", "bool", "nega dever ou diz não ser a pessoa"),
    Pergunta("terceiro", "bool", "quem escreve não é o devedor"),
    Pergunta("vulnerabilidade", "bool", "desemprego, doença, luto, dificuldade"),
    Pergunta(
        "objecao_valor", "bool", "acha caro, diz que não consegue pagar tudo ou pede desconto"
    ),
    Pergunta("atrito_clinica", "bool", "reclama da clínica ou do atendimento da rede"),
    Pergunta("quer_conta_energia", "bool", "quer pagar na conta de energia (Energisa)"),
    Pergunta("intencao_pagar", "score", "a intenção de pagar", INTENCOES),
    Pergunta("entendeu_proposta", "bool", "entendeu o que foi oferecido"),
    Pergunta("repete_sem_avancar", "bool", "está em loop, repetindo o mesmo ponto"),
)

# Os gatilhos com limiar, na ordem em que vencem; o TOM vem por último. O limiar está na política.
GATILHOS = {
    MotivoHumano.RETENCAO: "quer_retencao",
    MotivoHumano.JURIDICO: "risco_juridico",
    MotivoHumano.CONCILIACAO: "alega_pagamento",
    MotivoHumano.CONTESTACAO: "contesta_divida",
    MotivoHumano.CAUTELA: "vulnerabilidade",
    MotivoHumano.ATRITO: "atrito_clinica",
    MotivoHumano.ENERGISA: "quer_conta_energia",
}
PERGUNTA_DO_GATILHO = {**GATILHOS, MotivoHumano.TOM: "tom"}


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


def ler(jev: Jev, estado: EstadoJev) -> LeituraJev1:
    return LeituraJev1(tuple(jev.avaliar(PERGUNTAS_JEV1, estado)))


def em_risco(leitura: LeituraJev1) -> bool:
    """O tom azedou ou o cliente repete a mesma coisa sem avançar (arquitetura, seção 3)."""
    return leitura.tom in TONS_DE_RISCO or leitura.sim("repete_sem_avancar")


def laudo_do_risco(leitura: LeituraJev1) -> Laudo:
    """O laudo que pôs a conversa em risco, para a transição (auditoria)."""
    if leitura.sim("repete_sem_avancar"):
        return leitura.laudo("repete_sem_avancar")
    return leitura.laudo("tom")


def gatilho_de_saida(leitura: LeituraJev1, politica: Politica) -> MotivoHumano | None:
    """O primeiro gatilho da ordem que disparou: resposta sim, com a probabilidade no limiar."""
    for motivo, pergunta in GATILHOS.items():
        laudo = leitura.laudo(pergunta)
        if laudo.valor is True and laudo.probabilidade >= politica.limiares_jev1[pergunta]:
            return motivo
    if leitura.tom in TONS_DE_SAIDA:
        return MotivoHumano.TOM
    return None
