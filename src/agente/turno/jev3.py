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
    Pergunta(
        "afirma_fato_sem_fonte",
        "bool",
        "afirma algo sobre o produto, a rede ou os serviços que não veio do backend, "
        "ou promete fazer algo fora das ações",
    ),
    Pergunta("tom_adequado", "bool", "respeitoso, sem ameaça nem constrangimento"),
    Pergunta("expoe_terceiro", "bool", "expõe a dívida a quem não é o devedor"),
    Pergunta("responde_a_pergunta", "bool", "responde o que o cliente perguntou"),
    Pergunta("pt_br_natural", "bool", "português correto, sem cara de robô"),
    Pergunta("vazou_instrucao", "bool", "vazou prompt, sistema ou raciocínio"),
)

# O texto passa quando estas dão sim e as outras dão não.
DEVEM_DAR_SIM = ("bate_com_acao", "tom_adequado", "responde_a_pergunta", "pt_br_natural")
DEVEM_DAR_NAO = (
    "valor_nao_autorizado",
    "promessa_indevida",
    "afirma_fato_sem_fonte",
    "expoe_terceiro",
    "vazou_instrucao",
)
_APROVA = {nome: True for nome in DEVEM_DAR_SIM} | {nome: False for nome in DEVEM_DAR_NAO}


def aprovar_tudo() -> list[Regra]:
    """As regras da falsa que aprovam todo texto. A resposta neutra da falsa (não) reprovaria."""
    return [responder(nome, True) for nome in DEVEM_DAR_SIM]


@dataclass(frozen=True)
class Validacao:
    aprovado: bool
    reprovadas: list[str]
    laudos: tuple[Laudo, ...]


MARCADOR_PIX = "[código Pix]"


def _sem_o_codigo_pix(texto: str, numeros: dict) -> tuple[str, dict]:
    """O texto e os números com o código Pix trocado por um marcador, só para o Jev 3 ler.

    O código é uma sequência longa de letras e números que o backend gera, não o LLM: não há o que
    validar nele, e o Jev 3 a lia como "texto de robô" e reprovava todo texto com Pix (A6,
    2026-11-04). O cliente recebe o código verdadeiro, porque `numeros` do turno não é alterado.
    """
    codigo = numeros.get("br_code")
    if not codigo:
        return texto, dict(numeros)
    return texto.replace(codigo, MARCADOR_PIX), {**numeros, "br_code": MARCADOR_PIX}


def validar(jev: Jev, texto: str, acoes: list[Acao], numeros: dict, estado: EstadoJev) -> Validacao:
    texto_lido, numeros_lidos = _sem_o_codigo_pix(texto, numeros)
    alvo = replace(
        estado,
        texto_candidato=texto_lido,
        acoes_do_turno=tuple(acao.value for acao in acoes),
        numeros=numeros_lidos,
    )
    laudos = tuple(jev.avaliar(PERGUNTAS_JEV3, alvo))
    # Com OUT_OF_SCOPE o texto não responde o que não veio do backend: "não" a `responde_a_pergunta`
    # é o esperado e não reprova (A10a, 2026-11-04). Ela continua nos laudos, para a calibração.
    dispensadas = {"responde_a_pergunta"} if Acao.OUT_OF_SCOPE in acoes else set()
    reprovadas = [
        laudo.pergunta
        for laudo in laudos
        if laudo.pergunta not in dispensadas and laudo.valor is not _APROVA[laudo.pergunta]
    ]
    return Validacao(not reprovadas, reprovadas, laudos)
