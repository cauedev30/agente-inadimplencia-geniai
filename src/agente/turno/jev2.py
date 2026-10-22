"""Jev 2: a decisão da ação principal do turno (arquitetura, seção 4).

O Jev só responde perguntas tipadas, então a decisão é uma escolha entre as ações; o que vai junto é
anexado pelo código (D37). Nada de conta para o Jev: o degrau e a propensão vêm do backend.
"""

from dataclasses import replace

from agente.integracoes.jev import EstadoJev, Jev, Pergunta
from agente.turno.acoes import Acao, Decisao
from agente.turno.jev1 import LeituraJev1

PERGUNTAS_JEV2 = (
    Pergunta(
        "acao",
        "escolha",
        "a ação principal do turno: apresentar o débito, liberar o próximo degrau do desconto, "
        "mandar o link do CTN, gerar o Pix, fora de escopo, não agir ou mandar para o humano",
        tuple(acao.value for acao in Acao),
    ),
)


def decidir(jev: Jev, estado: EstadoJev, leitura: LeituraJev1) -> Decisao:
    [laudo] = jev.avaliar(PERGUNTAS_JEV2, replace(estado, leitura=leitura.laudos))
    return Decisao(Acao(laudo.valor), laudo.probabilidade, laudo.confianca)
