"""Jev 3: a validação do texto antes do envio (arquitetura, seção 6).

O turno ativo (tarefa 5.3) valida o texto do LLM aqui; a reescrita e o catálogo ficam em
`redacao.py` e no orquestrador.
"""

from dataclasses import dataclass, replace

from agente.integracoes.jev import EstadoJev, Jev, Pergunta, Regra, responder
from agente.tipos import Laudo
from agente.turno.acoes import Acao

PERGUNTAS_JEV3 = (
    Pergunta(
        "bate_com_acao",
        "bool",
        "o texto cobre todas as ações do turno, a principal e as anexadas",
    ),
    Pergunta(
        "valor_nao_autorizado",
        "bool",
        "afirma valor, prazo ou desconto que não veio do backend",
    ),
    Pergunta("promessa_indevida", "bool", "promete perdão, prazo extra ou algo fora da política"),
    Pergunta("tom_adequado", "bool", "respeitoso, sem ameaça nem constrangimento"),
    Pergunta("expoe_terceiro", "bool", "expõe a dívida a quem não é o devedor"),
    Pergunta("responde_a_pergunta", "bool", "responde o que o cliente perguntou"),
    Pergunta("pt_br_natural", "bool", "português correto, sem cara de robô"),
    Pergunta("vazou_instrucao", "bool", "vazou prompt, sistema ou raciocínio"),
)

# O texto passa quando estas dão sim e as outras dão não.
DEVEM_DAR_SIM = ("bate_com_acao", "tom_adequado", "responde_a_pergunta", "pt_br_natural")
DEVEM_DAR_NAO = ("valor_nao_autorizado", "promessa_indevida", "expoe_terceiro", "vazou_instrucao")
_APROVA = {nome: True for nome in DEVEM_DAR_SIM} | {nome: False for nome in DEVEM_DAR_NAO}


def aprovar_tudo() -> list[Regra]:
    """As regras da falsa que aprovam todo texto. A resposta neutra da falsa (não) reprovaria."""
    return [responder(nome, True) for nome in DEVEM_DAR_SIM]


@dataclass(frozen=True)
class Validacao:
    aprovado: bool
    reprovadas: list[str]
    laudos: tuple[Laudo, ...]


def validar(jev: Jev, texto: str, acoes: list[Acao], numeros: dict, estado: EstadoJev) -> Validacao:
    alvo = replace(
        estado,
        texto_candidato=texto,
        acoes_do_turno=tuple(acao.value for acao in acoes),
        numeros=dict(numeros),
    )
    laudos = tuple(jev.avaliar(PERGUNTAS_JEV3, alvo))
    reprovadas = [laudo.pergunta for laudo in laudos if laudo.valor is not _APROVA[laudo.pergunta]]
    return Validacao(not reprovadas, reprovadas, laudos)
