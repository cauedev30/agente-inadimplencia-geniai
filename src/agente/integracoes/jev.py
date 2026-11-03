"""Interface do Jev (TypeSafe AI): responde perguntas tipadas com probabilidade e confiança.

Como o Python chama o Jev depende da T10, e as contas, da T5. Até lá, o sistema roda com o JevFalso.
A implementação real liga Zero Data Retention e No Training em toda chamada (D12). O JevComPrazo
envolve qualquer Jev com o `jev_timeout_ms` e o contrato (arquitetura, seção 9).
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as TempoEsgotado
from dataclasses import dataclass, field
from typing import Literal, Protocol

from agente.dominio.desconto import Oferta
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo

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


@dataclass(frozen=True)
class EstadoJev:
    """O que o Jev lê: o histórico (origem, texto), a régua, a dívida, o estado e a oferta atual.

    O Jev 2 lê também a leitura do Jev 1 do mesmo turno; o Jev 3, o texto candidato, as ações do
    turno e os números do backend.
    """

    historico: tuple[tuple[str, str], ...]
    regua: Regua
    divida_centavos: int
    estado_conversa: EstadoConversa
    oferta_atual: Oferta | None
    leitura: tuple[Laudo, ...] = ()
    texto_candidato: str | None = None
    acoes_do_turno: tuple[str, ...] = ()
    numeros: Mapping[str, object] = field(default_factory=dict)


class JevIndisponivel(RuntimeError):
    """O Jev falhou, demorou além do `jev_timeout_ms` ou deixou pergunta sem laudo."""


class Jev(Protocol):
    # Quem respondeu: "falso" ou o identificador do Jev real. Separa os laudos na calibração.
    origem: str

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        """Um laudo por pergunta, na ordem das perguntas. Levanta JevIndisponivel."""
        ...


def conferir_laudos(perguntas: Sequence[Pergunta], laudos: Sequence[Laudo]) -> list[Laudo]:
    """Confere o contrato: um laudo por pergunta, na ordem, com o valor do tipo certo."""
    nomes = [laudo.pergunta for laudo in laudos]
    esperados = [pergunta.nome for pergunta in perguntas]
    if nomes != esperados:
        faltando = [nome for nome in esperados if nome not in nomes]
        raise JevIndisponivel(f"laudos fora do contrato; faltando: {', '.join(faltando) or '-'}")
    for pergunta, laudo in zip(perguntas, laudos, strict=True):
        if not pergunta.aceita(laudo.valor):
            raise JevIndisponivel(f"valor inválido para {pergunta.nome}: {laudo.valor!r}")
        if not (0 <= laudo.probabilidade <= 1 and 0 <= laudo.confianca <= 1):
            raise JevIndisponivel(f"probabilidade ou confiança fora de 0 a 1 em {pergunta.nome}")
    return list(laudos)


class JevComPrazo:
    """Erro, demora acima do prazo ou laudo fora do contrato viram JevIndisponivel."""

    def __init__(self, jev: Jev, timeout_ms: int):
        self.jev = jev
        self.timeout_ms = timeout_ms

    @property
    def origem(self) -> str:
        return self.jev.origem

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        futuro: Future[list[Laudo]] = Future()

        def chamar() -> None:
            try:
                futuro.set_result(self.jev.avaliar(perguntas, estado))
            except BaseException as erro:
                futuro.set_exception(erro)

        # Thread daemon: a chamada atrasada segue sem o turno e não segura o desligamento do
        # processo (a do ThreadPoolExecutor é juntada na saída). A real (T10) precisa de um
        # timeout HTTP próprio, menor que o `jev_timeout_ms`, para a chamada não ficar viva.
        threading.Thread(target=chamar, name="jev", daemon=True).start()
        try:
            return conferir_laudos(perguntas, futuro.result(timeout=self.timeout_ms / 1000))
        except TempoEsgotado:
            raise JevIndisponivel(f"o Jev passou de {self.timeout_ms} ms") from None
        except JevIndisponivel:
            raise
        except Exception as erro:
            # Inclui o laudo malformado da real (probabilidade que não é número, objeto errado).
            raise JevIndisponivel(f"o Jev falhou: {erro}") from erro


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
    """Responde pelas regras escritas no teste; a primeira regra que responde vence.

    Só grava as chamadas com `registrar`, que é coisa de teste: em produção, o estado traz o
    histórico do cliente, e a falsa não pode acumular dado pessoal na memória.
    """

    regras: Sequence[Regra] = ()
    demora_s: float = 0.0
    falha: Exception | None = None
    origem: str = "falso"
    registrar: bool = False
    chamadas: list[tuple[list[str], EstadoJev]] = field(default_factory=list)

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        if self.registrar:
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
