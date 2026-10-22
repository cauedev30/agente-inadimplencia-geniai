"""Comparação dos redatores para a escolha do LLM (tarefa 5.4, T6).

Cada candidato redige os mesmos roteiros da simulação, pelo turno ativo com as falsas. O relatório
traz a taxa de reprovação (na interpolação e no Jev 3), o tamanho dos textos, a latência e o custo
por conversa. Com o Jev falso, o Jev 3 aprova todo texto: só a interpolação reprova até a T10.
"""

import time
from dataclasses import dataclass, field, replace

from agente.integracoes.redator import PedidoRedacao, Redator, Uso
from agente.simulacao import Caso, DependenciasSimulacao, rodar_simulacao


@dataclass(frozen=True)
class Candidato:
    redator: Redator
    # O preço em US$ por milhão de tokens, para o custo. Sem o preço, o custo sai 0.
    preco_entrada: float = 0.0
    preco_saida: float = 0.0


@dataclass
class _Medidor:
    """Envolve o redator e soma as chamadas, o tempo e os tokens."""

    redator: Redator
    chamadas: int = 0
    segundos: float = 0.0
    uso: list[Uso] = field(default_factory=list)

    @property
    def nome(self) -> str:
        return self.redator.nome

    @property
    def ultimo_uso(self) -> Uso | None:
        return self.redator.ultimo_uso

    def redigir(self, pedido: PedidoRedacao) -> str:
        inicio = time.perf_counter()
        try:
            return self.redator.redigir(pedido)
        finally:
            self.segundos += time.perf_counter() - inicio
            self.chamadas += 1
            if self.redator.ultimo_uso is not None:
                self.uso.append(self.redator.ultimo_uso)


@dataclass(frozen=True)
class ResultadoRedator:
    nome: str
    tentativas: int
    reprovadas: int
    respostas_seguras: int
    divergencias: int
    tamanho_medio: float
    latencia_media_s: float
    tokens_por_conversa: float
    custo_por_conversa_usd: float

    @property
    def taxa_reprovacao(self) -> float:
        return self.reprovadas / self.tentativas if self.tentativas else 0.0


@dataclass
class RelatorioRedatores:
    casos: int
    resultados: list[ResultadoRedator] = field(default_factory=list)


def _media(valores: list[float]) -> float:
    return sum(valores) / len(valores) if valores else 0.0


def _avaliar(candidato: Candidato, casos: list[Caso], deps: DependenciasSimulacao):
    medidor = _Medidor(candidato.redator)
    relatorio = rodar_simulacao(casos, replace(deps, redator=medidor))
    entrada = sum(uso.tokens_entrada for uso in medidor.uso)
    saida = sum(uso.tokens_saida for uso in medidor.uso)
    custo = (entrada * candidato.preco_entrada + saida * candidato.preco_saida) / 1_000_000
    conversas = len(casos) or 1
    return ResultadoRedator(
        nome=candidato.redator.nome,
        tentativas=relatorio.tentativas,
        reprovadas=relatorio.reprovadas,
        respostas_seguras=relatorio.respostas_seguras,
        divergencias=len(relatorio.divergencias),
        tamanho_medio=_media([len(texto) for texto in relatorio.textos]),
        latencia_media_s=medidor.segundos / medidor.chamadas if medidor.chamadas else 0.0,
        tokens_por_conversa=(entrada + saida) / conversas,
        custo_por_conversa_usd=custo / conversas,
    )


def comparar_redatores(
    candidatos: list[Candidato], casos: list[Caso], deps: DependenciasSimulacao
) -> RelatorioRedatores:
    return RelatorioRedatores(
        casos=len(casos), resultados=[_avaliar(c, casos, deps) for c in candidatos]
    )


def ler_candidato(valor: str) -> tuple[str, float, float]:
    """`modelo` ou `modelo:entrada:saida`, com o preço em US$ por milhão de tokens."""
    partes = valor.split(":")
    if not partes[0]:
        raise ValueError("informe o modelo")
    if len(partes) == 1:
        return partes[0], 0.0, 0.0
    if len(partes) != 3:
        raise ValueError(f"use modelo ou modelo:entrada:saida, veio {valor!r}")
    try:
        return partes[0], float(partes[1]), float(partes[2])
    except ValueError:
        raise ValueError(f"o preço tem de ser um número, veio {valor!r}") from None


def formatar_comparacao(relatorio: RelatorioRedatores) -> str:
    linhas = [f"Comparação dos redatores: {relatorio.casos} casos", ""]
    for r in relatorio.resultados:
        linhas.append(
            f"  {r.nome}: reprovação {r.taxa_reprovacao:.0%} ({r.reprovadas}/{r.tentativas}), "
            f"respostas seguras {r.respostas_seguras}, divergências {r.divergencias}, "
            f"tamanho {r.tamanho_medio:.0f} caracteres, latência {r.latencia_media_s:.2f} s, "
            f"{r.tokens_por_conversa:.0f} tokens e custo US$ {r.custo_por_conversa_usd:.5f} "
            "por conversa"
        )
    return "\n".join(linhas)
