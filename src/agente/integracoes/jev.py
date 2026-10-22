"""Interface do Jev (TypeSafe AI): responde perguntas tipadas com probabilidade e confiança.

Como o Python chama o Jev depende da T10, e as contas, da T5. Até lá, o sistema roda com o JevFalso.
A implementação real liga Zero Data Retention e No Training em toda chamada (D12). O JevComPrazo
envolve qualquer Jev com o `jev_timeout_ms` e o contrato (arquitetura, seção 9).
"""

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as TempoEsgotado
from dataclasses import dataclass, field
from typing import Literal, Protocol

from agente.dominio.desconto import Oferta
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo

TipoPergunta = Literal["bool", "score", "escolha"]


@dataclass(frozen=True)
class Pergunta:
    """Uma pergunta tipada. A nota (score) e a escolha respondem com uma das opções."""

    nome: str
    tipo: TipoPergunta
    descricao: str
    opcoes: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.tipo == "bool" and self.opcoes is not None:
            raise ValueError(f"a pergunta {self.nome} é sim/não e não leva opções")
        if self.tipo != "bool" and not self.opcoes:
            raise ValueError(f"a pergunta {self.nome} ({self.tipo}) precisa das opções")


@dataclass(frozen=True)
class EstadoJev:
    """O que o Jev lê: o histórico (origem, texto), a régua, a dívida, o estado e a oferta atual."""

    historico: tuple[tuple[str, str], ...]
    regua: Regua
    divida_centavos: int
    estado_conversa: EstadoConversa
    oferta_atual: Oferta | None


class JevIndisponivel(RuntimeError):
    """O Jev falhou, demorou além do `jev_timeout_ms` ou deixou pergunta sem laudo."""


class Jev(Protocol):
    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        """Um laudo por pergunta, na ordem das perguntas. Levanta JevIndisponivel."""
        ...


def _valor_valido(pergunta: Pergunta, valor) -> bool:
    if pergunta.tipo == "bool":
        return isinstance(valor, bool)
    return valor in (pergunta.opcoes or ())


def conferir_laudos(perguntas: Sequence[Pergunta], laudos: Sequence[Laudo]) -> list[Laudo]:
    """Confere o contrato: um laudo por pergunta, na ordem, com o valor do tipo certo."""
    nomes = [laudo.pergunta for laudo in laudos]
    esperados = [pergunta.nome for pergunta in perguntas]
    if nomes != esperados:
        faltando = [nome for nome in esperados if nome not in nomes]
        raise JevIndisponivel(f"laudos fora do contrato; faltando: {', '.join(faltando) or '-'}")
    for pergunta, laudo in zip(perguntas, laudos, strict=True):
        if not _valor_valido(pergunta, laudo.valor):
            raise JevIndisponivel(f"valor inválido para {pergunta.nome}: {laudo.valor!r}")
        if not (0 <= laudo.probabilidade <= 1 and 0 <= laudo.confianca <= 1):
            raise JevIndisponivel(f"probabilidade ou confiança fora de 0 a 1 em {pergunta.nome}")
    return list(laudos)


class JevComPrazo:
    """Erro, demora acima do prazo ou laudo fora do contrato viram JevIndisponivel."""

    def __init__(self, jev: Jev, timeout_ms: int):
        self._jev = jev
        self._timeout_ms = timeout_ms

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        executor = ThreadPoolExecutor(max_workers=1)
        futuro = executor.submit(self._jev.avaliar, perguntas, estado)
        try:
            laudos = futuro.result(timeout=self._timeout_ms / 1000)
        except TempoEsgotado:
            raise JevIndisponivel(f"o Jev passou de {self._timeout_ms} ms") from None
        except JevIndisponivel:
            raise
        except Exception as erro:
            raise JevIndisponivel(f"o Jev falhou: {erro}") from erro
        finally:
            # Não espera a chamada atrasada: o turno segue sem ela.
            executor.shutdown(wait=False)
        return conferir_laudos(perguntas, laudos)


Regra = Callable[[Pergunta, EstadoJev], Laudo | None]


def responder(
    pergunta: str,
    valor: bool | float | str,
    probabilidade: float = 0.95,
    confianca: float = 0.90,
    se: Callable[[EstadoJev], bool] | None = None,
) -> Regra:
    """Uma regra da falsa: responde `valor` à pergunta, quando a condição vale."""

    def regra(alvo: Pergunta, estado: EstadoJev) -> Laudo | None:
        if alvo.nome != pergunta or (se is not None and not se(estado)):
            return None
        return Laudo(pergunta, valor, probabilidade, confianca)

    return regra


def laudo_neutro(pergunta: Pergunta) -> Laudo:
    """A resposta da falsa sem regra: não, ou a primeira opção, com certeza alta."""
    valor = False if pergunta.tipo == "bool" else (pergunta.opcoes or ("",))[0]
    return Laudo(pergunta.nome, valor, 0.95, 0.90)


@dataclass
class JevFalso:
    """Responde pelas regras escritas no teste; a primeira regra que responde vence."""

    regras: Sequence[Regra] = ()
    demora_s: float = 0.0
    falha: Exception | None = None
    chamadas: list[tuple[list[str], EstadoJev]] = field(default_factory=list)

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        self.chamadas.append(([pergunta.nome for pergunta in perguntas], estado))
        if self.demora_s:
            time.sleep(self.demora_s)
        if self.falha is not None:
            raise self.falha
        return [self._responder(pergunta, estado) for pergunta in perguntas]

    def _responder(self, pergunta: Pergunta, estado: EstadoJev) -> Laudo:
        for regra in self.regras:
            laudo = regra(pergunta, estado)
            if laudo is not None:
                return laudo
        return laudo_neutro(pergunta)
