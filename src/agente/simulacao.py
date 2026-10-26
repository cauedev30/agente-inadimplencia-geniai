"""Conversas simuladas (tarefa 4.6): roteiros em YAML passados pelo turno ativo, com as falsas.

Cada turno traz a fala do cliente, o gabarito do Jev (`perguntas`) e o esperado: a ação com os
anexos, ou o gatilho, que é o motivo da saída para o humano. Com a falsa, o Jev responde o gabarito
e o relatório testa o encanamento; com o Jev real (T10), o mesmo relatório serve para calibrar os
limiares. As conversas da Lohanny entram aqui quando chegarem.

Cada caso roda pelo mesmo `processar_turno` do serviço, no modo ativo (tarefa 5.3): o backend
executa, o redator escreve e o Jev 3 valida. O banco é em memória, e o Chatwoot, o CTN e o Pix são
os falsos: nada sai de verdade.
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Base, Cliente, Cobranca, Conversa, LaudoGravado, Turno
from agente.db.sessao import criar_sessao
from agente.dominio.divida import calcular_divida
from agente.dominio.janela import proxima_abertura
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import Jev, JevFalso, responder
from agente.integracoes.pix import PixFalso
from agente.integracoes.redator import Redator, RedatorFalso
from agente.politica import Politica, Regua
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno.acoes import Acao
from agente.turno.avaliacao import NAO_EXECUTADA, rotulo_humano
from agente.turno.jev1 import PERGUNTAS_JEV1
from agente.turno.jev2 import PERGUNTAS_JEV2
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

_PERGUNTAS = {pergunta.nome: pergunta for pergunta in (*PERGUNTAS_JEV1, *PERGUNTAS_JEV2)}


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
        return self.acao.value if self.acao is not None else rotulo_humano(self.gatilho)


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
            if not pergunta.aceita(valor):
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
    # Sem Jev, cada turno usa a falsa respondendo o gabarito e aprovando o texto no Jev 3.
    jev: Jev | None = None
    # Sem redator, o falso.
    redator: Redator | None = None


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
    # Os textos do LLM aprovados e enviados ao cliente, na ordem.
    textos: list[str] = field(default_factory=list)
    # As tentativas do LLM, as que reprovaram (na interpolação ou no Jev 3) e as respostas seguras.
    tentativas: int = 0
    reprovadas: int = 0
    respostas_seguras: int = 0


def _respostas(sessao: Session, turno: Turno) -> dict:
    """O que o Jev 1 e o Jev 2 responderam no turno, para comparar com o gabarito."""
    laudos = sessao.scalars(
        select(LaudoGravado).where(
            LaudoGravado.turno_id == turno.id, LaudoGravado.batch != BatchJev.JEV3
        )
    )
    return {laudo.pergunta: laudo.valor for laudo in laudos}


def _conferir(
    caso: str, numero: int, turno: TurnoCaso, respostas: dict, gravado: Turno, relatorio
) -> None:
    """Compara o turno gravado com o gabarito e o esperado, e conta os acertos."""
    for nome, valor in turno.perguntas.items():
        relatorio.por_pergunta[nome].contar(respostas.get(nome) == valor)

    esperado = turno.esperado
    # O turno cujo texto não saiu grava a ação decidida com o prefixo: a decisão do Jev 2 vale.
    rotulo = str(gravado.acao_principal).removeprefix(NAO_EXECUTADA)
    acertou_acao = rotulo == esperado.rotulo
    acertou_anexos = gravado.acoes_anexadas == esperado.anexos
    relatorio.por_acao[esperado.rotulo].contar(acertou_acao and acertou_anexos)
    if not acertou_acao:
        relatorio.divergencias.append(Divergencia(caso, numero, "acao", esperado.rotulo, rotulo))
    elif not acertou_anexos:
        anexos = (", ".join(esperado.anexos) or "-", ", ".join(gravado.acoes_anexadas) or "-")
        relatorio.divergencias.append(Divergencia(caso, numero, "anexos", *anexos))


def _contar_redacao(gravado: Turno, relatorio: RelatorioSimulacao) -> None:
    tentativas = gravado.redacao.get("tentativas", [])
    relatorio.tentativas += len(tentativas)
    relatorio.reprovadas += sum(bool(tentativa["reprovadas"]) for tentativa in tentativas)
    saida = gravado.redacao.get("saida")
    if saida == "LLM":
        relatorio.textos.append(gravado.texto_enviado)
    elif saida == "RESPOSTA_SEGURA":
        relatorio.respostas_seguras += 1


def _rodar_caso(
    sessao: Session,
    indice: int,
    caso: Caso,
    deps: DependenciasSimulacao,
    relatorio: RelatorioSimulacao,
    chatwoot: ChatwootFalso,
    ctn: CTNFalso,
) -> None:
    politica = deps.politica
    cliente = Cliente(
        matricula_ctn=f"SIM-{indice:04d}", nome="Cliente Simulado", telefone=f"+5583900{indice:06d}"
    )
    conversa = Conversa(
        chatwoot_conversa_id=indice,
        cobranca=Cobranca(
            cliente=cliente,
            regua=caso.regua,
            mensalidades_abertas=caso.mensalidades,
            divida_centavos=calcular_divida(caso.mensalidades, politica),
            propenso_cartao=caso.propenso,
            campanha="simulacao",
        ),
    )
    sessao.add(conversa)
    # Commit, como no Trabalhador: o rollback do erro interno só desfaz o turno, não o caso.
    sessao.commit()
    # Uma segunda-feira às 10h, ou a próxima abertura da janela, se a política a fechar.
    agora = proxima_abertura(datetime(2026, 10, 26, 10, tzinfo=politica.janela.zona), politica)
    inicio = agora
    ctn.clientes[cliente.matricula_ctn] = ClienteCTN(
        cliente.matricula_ctn, cliente.nome, cliente.telefone, None
    )
    base = Dependencias(
        # Toda conversa simulada roda no modo ativo, seja qual for a liberação do arquivo.
        politica=liberar_tudo(politica),
        chatwoot=chatwoot,
        disparador=DisparadorFalso(),
        time_humano=None,
        modo=Modo.ATIVO,
        jev=JevFalso(),
        ctn=ctn,
        pix=PixFalso(agora=lambda: inicio),
        redator=deps.redator or RedatorFalso(),
    )
    for numero, turno in enumerate(caso.turnos, start=1):
        relatorio.turnos += 1
        agora += timedelta(minutes=1)
        registrar_mensagem(
            sessao, conversa.id, OrigemMensagem.CLIENTE, turno.cliente, None, False, agora
        )
        sessao.commit()
        gabarito = [responder(nome, valor) for nome, valor in turno.perguntas.items()]
        jev = deps.jev or JevFalso([*gabarito, *aprovar_tudo()])
        resultado = processar_turno(sessao, conversa.id, agora, replace(base, jev=jev))

        gravado = sessao.scalars(
            select(Turno).where(Turno.conversa_id == conversa.id).order_by(Turno.id.desc())
        ).first()
        if resultado is ResultadoTurno.ERRO:
            # Erro de código no turno (só o tipo é gravado): o caso não segue.
            erro = str(gravado.redacao.get("erro", "desconhecido"))
            relatorio.divergencias.append(
                Divergencia(caso.nome, numero, "erro", "turno sem erro", erro)
            )
            return
        _conferir(caso.nome, numero, turno, _respostas(sessao, gravado), gravado, relatorio)
        _contar_redacao(gravado, relatorio)
        if conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO:
            if numero < len(caso.turnos):
                relatorio.divergencias.append(
                    Divergencia(caso.nome, numero + 1, "caso", "seguir", f"parou no turno {numero}")
                )
            return


def rodar_simulacao(casos: list[Caso], deps: DependenciasSimulacao) -> RelatorioSimulacao:
    relatorio = RelatorioSimulacao(casos=len(casos))
    fabrica = criar_sessao("sqlite://")
    motor = fabrica.kw["bind"]
    Base.metadata.create_all(motor)
    try:
        with fabrica() as sessao:
            # Um Chatwoot só: o id de cada mensagem é único no banco, como no Chatwoot de verdade.
            chatwoot = ChatwootFalso()
            # E um CTN só: a referência do link é única no banco (`pagamento.referencia`).
            ctn = CTNFalso()
            for indice, caso in enumerate(casos, start=1):
                _rodar_caso(sessao, indice, caso, deps, relatorio, chatwoot, ctn)
    finally:
        motor.dispose()
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
