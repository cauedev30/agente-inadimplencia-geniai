"""Conversas simuladas (tarefa 4.6): roteiros em YAML passados pelo Jev e pelas regras de ação.

Cada turno traz a fala do cliente, o gabarito do Jev (`perguntas`) e o esperado: a ação com os
anexos, ou o gatilho, que é o motivo da saída para o humano. Com a falsa, o Jev responde o gabarito
e o relatório testa o encanamento; com o Jev real (T10), o mesmo relatório serve para calibrar os
limiares. As conversas da Lohanny entram aqui quando chegarem.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.divida import calcular_divida
from agente.integracoes.ctn import CTN, ClienteCTN, CTNFalso
from agente.integracoes.jev import Jev, JevFalso, responder
from agente.integracoes.pix import PixBanco, PixFalso
from agente.politica import Politica, Regua
from agente.tipos import EstadoConversa, MotivoHumano
from agente.turno.acoes import Acao
from agente.turno.avaliacao import Avaliacao, avaliar, estado_jev
from agente.turno.executar import executar
from agente.turno.jev1 import PERGUNTAS_JEV1
from agente.turno.jev2 import PERGUNTAS_JEV2

_PERGUNTAS = {pergunta.nome: pergunta for pergunta in (*PERGUNTAS_JEV1, *PERGUNTAS_JEV2)}
# O momento da simulação só data o Pix falso; nenhum gate roda aqui.
_AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)


class _Imutavel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Esperado(_Imutavel):
    gatilho: MotivoHumano | None = None
    acao: Acao | None = None
    anexos: list[Acao] = []

    @model_validator(mode="after")
    def _um_dos_dois(self):
        if (self.gatilho is None) == (self.acao is None):
            raise ValueError("informe o gatilho ou a ação, não os dois")
        if self.gatilho is not None and self.anexos:
            raise ValueError("a saída para o humano não leva anexos")
        return self

    @property
    def rotulo(self) -> str:
        return self.acao.value if self.acao is not None else f"HUMAN:{self.gatilho.value}"


class TurnoCaso(_Imutavel):
    cliente: str
    perguntas: dict[str, bool | str] = {}
    esperado: Esperado

    @model_validator(mode="after")
    def _gabarito_valido(self):
        for nome, valor in self.perguntas.items():
            pergunta = _PERGUNTAS.get(nome)
            if pergunta is None:
                raise ValueError(f"pergunta desconhecida: {nome}")
            if pergunta.tipo == "bool" and not isinstance(valor, bool):
                raise ValueError(f"valor inválido para {nome}: {valor!r} (sim/não)")
            if pergunta.tipo != "bool" and valor not in (pergunta.opcoes or ()):
                raise ValueError(f"valor inválido para {nome}: {valor!r}")
        return self


class Caso(_Imutavel):
    nome: str
    regua: Regua
    mensalidades: int = Field(ge=1)
    propenso: bool
    turnos: list[TurnoCaso] = Field(min_length=1)

    @model_validator(mode="after")
    def _saida_no_fim(self):
        if any(turno.esperado.gatilho for turno in self.turnos[:-1]):
            raise ValueError("a saída para o humano encerra o caso: tem de ser o último turno")
        return self


class CasoInvalido(ValueError):
    """Um roteiro com um campo errado; a mensagem diz o arquivo e o campo."""


def _ler_arquivo(caminho: Path) -> list[Caso]:
    dados = yaml.safe_load(caminho.read_text(encoding="utf-8"))
    try:
        return [
            Caso.model_validate(caso) for caso in (dados if isinstance(dados, list) else [dados])
        ]
    except ValidationError as erro:
        campos = "; ".join(
            f"{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}" for e in erro.errors()
        )
        raise CasoInvalido(f"{caminho}: {campos}") from erro


def carregar_casos(caminho: Path) -> list[Caso]:
    """Os casos de um arquivo YAML, ou de todos os `*.yaml` de uma pasta."""
    arquivos = sorted(caminho.glob("*.yaml")) if caminho.is_dir() else [caminho]
    return [caso for arquivo in arquivos for caso in _ler_arquivo(arquivo)]


@dataclass
class DependenciasSimulacao:
    politica: Politica
    # Sem Jev, cada turno usa a falsa respondendo o gabarito.
    jev: Jev | None = None


@dataclass
class _Backend:
    """O que o `executar` usa no caso: o CTN falso conhece o cliente simulado."""

    politica: Politica
    ctn: CTN
    pix: PixBanco


@dataclass
class Acerto:
    acertos: int = 0
    total: int = 0

    def contar(self, acertou: bool) -> None:
        self.total += 1
        self.acertos += acertou


@dataclass(frozen=True)
class Divergencia:
    caso: str
    turno: int
    campo: str
    esperado: str
    obtido: str


@dataclass
class RelatorioSimulacao:
    casos: int = 0
    turnos: int = 0
    por_pergunta: dict[str, Acerto] = field(default_factory=lambda: defaultdict(Acerto))
    por_acao: dict[str, Acerto] = field(default_factory=lambda: defaultdict(Acerto))
    divergencias: list[Divergencia] = field(default_factory=list)


def _respostas(avaliacao: Avaliacao) -> dict:
    laudos = list(avaliacao.leitura.laudos) if avaliacao.leitura is not None else []
    if avaliacao.decisao is not None:
        laudos.append(avaliacao.decisao.laudo)
    return {laudo.pergunta: laudo.valor for laudo in laudos}


def _conferir(
    caso: str, numero: int, turno: TurnoCaso, avaliacao: Avaliacao, relatorio: RelatorioSimulacao
) -> None:
    """Compara o turno com o gabarito e o esperado, e conta os acertos."""
    respostas = _respostas(avaliacao)
    for nome, valor in turno.perguntas.items():
        relatorio.por_pergunta[nome].contar(respostas.get(nome) == valor)

    esperado = turno.esperado
    acertou_acao = avaliacao.rotulo == esperado.rotulo
    acertou_anexos = avaliacao.anexos == esperado.anexos
    relatorio.por_acao[esperado.rotulo].contar(acertou_acao and acertou_anexos)
    if not acertou_acao:
        relatorio.divergencias.append(
            Divergencia(caso, numero, "acao", esperado.rotulo, avaliacao.rotulo)
        )
    elif not acertou_anexos:
        anexos = (", ".join(esperado.anexos) or "-", ", ".join(avaliacao.anexos) or "-")
        relatorio.divergencias.append(Divergencia(caso, numero, "anexos", *anexos))


def _rodar_caso(
    indice: int, caso: Caso, deps: DependenciasSimulacao, relatorio: RelatorioSimulacao
) -> None:
    politica = deps.politica
    cliente = Cliente(matricula_ctn=f"SIM-{indice:04d}", nome="Cliente Simulado", telefone="")
    cobranca = Cobranca(
        id=indice,
        cliente=cliente,
        regua=caso.regua,
        mensalidades_abertas=caso.mensalidades,
        divida_centavos=calcular_divida(caso.mensalidades, politica),
        propenso_cartao=caso.propenso,
    )
    conversa = Conversa(estado=EstadoConversa.INICIADO, degrau_atual=0, objecoes_sem_desconto=0)
    ctn = CTNFalso([ClienteCTN(cliente.matricula_ctn, cliente.nome, cliente.telefone, None)])
    backend = _Backend(politica, ctn, PixFalso(agora=lambda: _AGORA))
    historico: list[tuple[str, str]] = []
    for numero, turno in enumerate(caso.turnos, start=1):
        relatorio.turnos += 1
        historico.append(("CLIENTE", turno.cliente))
        gabarito = [responder(nome, valor) for nome, valor in turno.perguntas.items()]
        jev = deps.jev or JevFalso(gabarito)
        estado = estado_jev(conversa, cobranca, historico, politica)
        avaliacao = avaliar(jev, estado, conversa, cobranca, politica)

        _conferir(caso.nome, numero, turno, avaliacao, relatorio)
        if avaliacao.motivo is not None:
            if numero < len(caso.turnos):
                relatorio.divergencias.append(
                    Divergencia(caso.nome, numero + 1, "caso", "seguir", f"parou no turno {numero}")
                )
            return
        # O mesmo backend do turno ativo avança o degrau e as objeções de um turno ao outro.
        executar(
            [avaliacao.acao, *avaliacao.anexos],
            cobranca,
            conversa,
            backend,
            _AGORA,
            objecao=avaliacao.leitura.sim("objecao_valor"),
        )
        historico.append(
            ("AGENTE", f"[teria feito: {' + '.join([avaliacao.rotulo, *avaliacao.anexos])}]")
        )


def rodar_simulacao(casos: list[Caso], deps: DependenciasSimulacao) -> RelatorioSimulacao:
    relatorio = RelatorioSimulacao(casos=len(casos))
    for indice, caso in enumerate(casos, start=1):
        _rodar_caso(indice, caso, deps, relatorio)
    return relatorio


def _linha(nome: str, acerto: Acerto) -> str:
    return f"  {nome}: {acerto.acertos}/{acerto.total} ({acerto.acertos / acerto.total:.0%})"


def formatar_relatorio(relatorio: RelatorioSimulacao) -> str:
    linhas = [
        f"Simulação: {relatorio.casos} casos, {relatorio.turnos} turnos, "
        f"{len(relatorio.divergencias)} divergências",
        "",
        "Acertos por pergunta:",
        *(_linha(nome, a) for nome, a in sorted(relatorio.por_pergunta.items())),
        "",
        "Acertos por ação:",
        *(_linha(nome, a) for nome, a in sorted(relatorio.por_acao.items())),
        "",
    ]
    if not relatorio.divergencias:
        linhas.append("Divergências: nenhuma")
    else:
        linhas.append("Divergências:")
        linhas += [
            f"  {d.caso}, turno {d.turno}, {d.campo}: esperado {d.esperado}, obtido {d.obtido}"
            for d in relatorio.divergencias
        ]
    return "\n".join(linhas)
