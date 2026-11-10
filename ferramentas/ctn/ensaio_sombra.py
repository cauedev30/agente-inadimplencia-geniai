"""Ensaio em sombra do CTN real (PARADA 3; plano `specs/planos/2026-11-06-integracao-ctn.md`).

Roda, contra a API real da EFICAZ e um banco DESCARTÁVEL em `pre-sombra/`, o que o serviço faria com
o CTN: importar o retrato do dia, conferir cobranças ATIVAS e ler as baixas (uma vez, ou em ciclos
de 10 minutos para medir o atraso entre `coletado_em` e a baixa aparecer, que decide a
`MARGEM_DE_RELEITURA`). NADA é enviado: o Chatwoot é o `ChatwootFalso` (o aviso de baixa vira um
contador) e o CTN só é lido (GET). A saída traz SÓ contagens, formatos e matrículas mascaradas
(últimos 3 dígitos): nunca telefone, nome, CPF, valor de cliente, id de baixa nem a chave.

Uso, de dentro de repo/ (o `uv` não está no PATH; use `python -m uv`):

    python -m uv run python ferramentas/ctn/ensaio_sombra.py --banco ../pre-sombra/parada3/ensaio.db
    python -m uv run python ferramentas/ctn/ensaio_sombra.py --banco ... --fase importar
    python -m uv run python ferramentas/ctn/ensaio_sombra.py --banco ... --fase conferir \
        --conferir 5
    python -m uv run python ferramentas/ctn/ensaio_sombra.py --banco ... --fase baixas
    python -m uv run python ferramentas/ctn/ensaio_sombra.py --banco ... --fase ciclos \\
        --ciclos 6 --intervalo-min 10 --relatorio ../pre-sombra/parada3/relatorio.txt

Sem `--fase`: importar, conferir e baixas, nessa ordem. `ciclos` só roda quando pedida (leva
`(ciclos - 1) x intervalo-min` minutos; não use o mesmo dia de outra leitura grande). A chave vem da
variável `AGENTE_CTN_API_KEY` ou de `<segredos>/eficaz.local.json` (como em `conferir_leitura.py`).

Padrões assumidos (dúvidas que o código e os documentos não resolvem; todas pelo lado seguro):
- O banco e o relatório são recusados dentro de `repo/`; o banco também se o caminho não tiver uma
  pasta `pre-sombra`. Um banco que já existe é reaproveitado (migrado até o fim), não apagado: assim
  `baixas` e `ciclos` rodam sobre o que o `importar` gravou.
- O serviço roda em SOMBRA (`Modo.SOMBRA`): a quitação grava a cobrança PAGA mas não toca na
  conversa nem envia aviso. Cobranças importadas não têm conversa, então a quitação sai como
  `quitou_sem_conversa`.
- Os tetos de cota (`--teto-linhas`, `--teto-chamadas`) valem para a execução INTEIRA, e a
  checagem é antes de cada chamada: o teto pode passar por até uma página (50 linhas). Ao
  passar, a fase aborta (o CTN vira "fora" para o código do serviço), as seguintes não rodam
  e o relatório sai parcial. A fase `importar` nem começa se o que resta da cota for menor que
  o custo esperado.
- Atraso da baixa = (instante do ciclo que a viu) - `coletado_em`. O 1º ciclo é a carga inicial (a
  rotina recua 3 h e traz o que já existia): sai à parte e NÃO entra na distribuição que decide a
  margem. Só mede o que a leitura enxerga (`coletado_em` >= cursor); a baixa que a API mostrar tarde
  com `coletado_em` anterior ao cursor não aparece aqui.
- A conferência usa `conferir_cobranca` (o miolo de `conferir_antes_do_envio`) para ver o motivo
  exato, e confere SEMPRE (ignora o "já conferida hoje"), porque o ensaio quer gastar a cota de
  propósito. Escolhe as primeiras N ATIVAS por id.
"""

import argparse
import importlib.util
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

from agente.carteira.conferir import conferir_cobranca
from agente.carteira.importar_ctn import (
    SEM_ENCERRAR_INCOMPLETO,
    ResumoImportacao,
    importar_do_ctn,
)
from agente.db.modelos import BaixaCTNRegistro, Cliente, Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import CTNIndisponivel
from agente.integracoes.ctn_eficaz import (
    FUSO_DO_NEGOCIO,
    UNIDADE_PADRAO,
    URL_EFICAZ,
    CTNEficaz,
)
from agente.pagamento.baixas_ctn import (
    ABATEU,
    MARGEM_DE_RELEITURA,
    QUITOU,
    DependenciasDasBaixas,
    ResumoBaixas,
    ler_baixas,
)
from agente.politica import Politica, carregar_politica
from agente.reproducao.reproduzir import migrar
from agente.tipos import Modo, StatusCobranca

# O script irmão (PARADA 1) não é um pacote: carrega o arquivo, sem mexer no sys.path.
_CAMINHO_IRMAO = Path(__file__).with_name("conferir_leitura.py")
_spec_irmao = importlib.util.spec_from_file_location("ctn_conferir_leitura_irmao", _CAMINHO_IRMAO)
_irmao = importlib.util.module_from_spec(_spec_irmao)
sys.modules[_spec_irmao.name] = _irmao
_spec_irmao.loader.exec_module(_irmao)

ErroDaConferencia = _irmao.ErroDaConferencia
Gravador = _irmao.Gravador
ler_chave = _irmao.ler_chave
mascarar = _irmao.mascarar
WORKSPACE = _irmao.WORKSPACE

RAIZ_DO_REPO = Path(__file__).resolve().parents[2]
PASTA_OBRIGATORIA = "pre-sombra"

FASES = ("importar", "conferir", "baixas", "ciclos")
FASES_PADRAO = ("importar", "conferir", "baixas")

TETO_DE_LINHAS = 5000
TETO_DE_CHAMADAS = 400
# O retrato inteiro, medido em 09/11 (`repo/docs/integracoes.md`, "O limite de taxa").
CHAMADAS_ESPERADAS_DO_RETRATO = 58
LINHAS_ESPERADAS_DO_RETRATO = 2900
COBRANCAS_A_CONFERIR = 5
CICLOS = 6
INTERVALO_EM_MINUTOS = 10.0
EXEMPLOS_DE_BAIXA = 5
CAMPANHA = "ctn"


def _de_interesse(efeito: str | None) -> bool:
    """A quitação (`quitou`, `quitou_sem_conversa`, ...) e o abate vêm primeiro nos exemplos: são o
    que a PARADA 3 quer ver na cobrança certa."""
    return efeito is not None and (efeito.startswith(QUITOU) or efeito == ABATEU)


class TetoExcedido(CTNIndisponivel):
    """A cota do ensaio acabou. Sendo um CTN "fora" para o código do serviço, a rotina para de
    chamar sozinha e nada anda (o cursor, o retrato). A mensagem não leva dado nenhum."""


class ChatwootSemEnvio(ChatwootFalso):
    """O Chatwoot do ensaio: não tem rede. Os avisos e as notas viram só um contador."""

    @property
    def avisos(self) -> int:
        return sum(1 for chamada in self.chamadas if chamada[0] == "enviar_mensagem")

    @property
    def notas(self) -> int:
        return sum(1 for chamada in self.chamadas if chamada[0] == "nota_privada")


class Medidor(Gravador):
    """O transporte do CTN do ensaio: conta chamadas e linhas devolvidas e recusa a chamada que
    passaria dos tetos. Herda do `Gravador` da PARADA 1 (que também resume formatos)."""

    def __init__(self, interno: httpx.BaseTransport, teto_chamadas: int, teto_linhas: int):
        super().__init__(interno, teto_chamadas)
        self.teto_chamadas = teto_chamadas
        self.teto_linhas = teto_linhas
        self.linhas = 0
        self.linhas_por_rota: Counter = Counter()
        self.estourou: str | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        motivo = None
        if self.chamadas >= self.teto_chamadas:
            motivo = f"teto de {self.teto_chamadas} chamadas atingido"
        elif self.linhas >= self.teto_linhas:
            motivo = f"teto de {self.teto_linhas} linhas atingido ({self.linhas} lidas)"
        if motivo is not None:
            self.estourou = self.estourou or motivo
            raise TetoExcedido(motivo)
        return super().handle_request(request)

    def _registrar(self, rota: str, conteudo: bytes) -> None:
        super()._registrar(rota, conteudo)
        try:
            corpo = json.loads(conteudo)
        except ValueError:
            return
        dados = corpo.get("dados") if isinstance(corpo, dict) else None
        if isinstance(dados, list):
            self.linhas += len(dados)
            self.linhas_por_rota[rota] += len(dados)


# ---- os caminhos ----


def validar_caminho(
    caminho: Path, raiz_do_repo: Path = RAIZ_DO_REPO, *, exigir_pre_sombra: bool = True
) -> Path:
    """O caminho absoluto, ou `ErroDaConferencia` se cair dentro de `repo/` (o banco guarda dado
    de cliente e não vai ao git) ou, para o banco, se não tiver uma pasta `pre-sombra`."""
    resolvido = caminho.expanduser().resolve()
    raiz = raiz_do_repo.resolve()
    if resolvido == raiz or raiz in resolvido.parents:
        raise ErroDaConferencia("o caminho não pode ficar dentro de repo/ (use pre-sombra/)")
    if exigir_pre_sombra and PASTA_OBRIGATORIA not in {p.lower() for p in resolvido.parts}:
        raise ErroDaConferencia(f"o banco precisa ficar numa pasta {PASTA_OBRIGATORIA}/")
    return resolvido


# ---- o atraso das baixas ----


def _como_utc(instante: datetime) -> datetime:
    return instante if instante.tzinfo is not None else instante.replace(tzinfo=UTC)


def atraso_em_minutos(visto_em: datetime, coletado_em: datetime) -> float:
    return (_como_utc(visto_em) - _como_utc(coletado_em)).total_seconds() / 60


def estatisticas_do_atraso(minutos: Sequence[float]) -> dict[str, float] | None:
    """Mínimo, mediana, p90 (posto mais próximo), máximo e contagem; `None` sem amostra."""
    if not minutos:
        return None
    ordenados = sorted(minutos)
    posto = max(0, math.ceil(0.9 * len(ordenados)) - 1)
    return {
        "n": len(ordenados),
        "min": ordenados[0],
        "mediana": statistics.median(ordenados),
        "p90": ordenados[posto],
        "max": ordenados[-1],
    }


def formatar_atraso(minutos: Sequence[float]) -> str:
    estatistica = estatisticas_do_atraso(minutos)
    if estatistica is None:
        return "nenhuma baixa nova"
    return (
        f"n={estatistica['n']:.0f}, min={estatistica['min']:.1f}, "
        f"mediana={estatistica['mediana']:.1f}, p90={estatistica['p90']:.1f}, "
        f"max={estatistica['max']:.1f} (minutos)"
    )


def _contagem(contador: Counter) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(contador.items())) or "-"


# ---- o contexto e o resultado ----


@dataclass
class _Contexto:
    fabrica: Callable
    ctn: CTNEficaz
    medidor: Medidor
    politica: Politica
    deps: DependenciasDasBaixas
    agora: Callable[[], datetime]
    dormir: Callable[[float], None]
    progresso: Callable[[str], None]
    campanha: str
    data_ref: date
    conferir: int
    ciclos: int
    intervalo_min: float


@dataclass
class _Fase:
    linhas: list[str]
    ok: bool = True


@dataclass
class _Nova:
    """Uma baixa gravada na passada, sem nada de pessoal."""

    coletado_em: datetime
    efeito: str | None
    casada: bool


@dataclass
class _Passada:
    visto_em: datetime
    resumo: ResumoBaixas
    novas: list[_Nova]
    exemplos: list[str]
    chamadas: int
    linhas: int


@dataclass
class Resultado:
    texto: str
    codigo: int
    chatwoot: ChatwootSemEnvio
    medidor: Medidor = field(repr=False)


def _custo(ctx: _Contexto, antes: tuple[int, int]) -> str:
    chamadas = ctx.medidor.chamadas - antes[0]
    linhas = ctx.medidor.linhas - antes[1]
    return f"  custo da fase: {chamadas} chamadas, {linhas} linhas"


def _marca(ctx: _Contexto) -> tuple[int, int]:
    return ctx.medidor.chamadas, ctx.medidor.linhas


# ---- fase 1: importar ----


def _linhas_do_resumo(resumo: ResumoImportacao) -> list[str]:
    linhas = [
        f"  criadas: {resumo.criadas}",
        f"  atualizadas: {resumo.atualizadas}",
        f"  reativadas: {resumo.reativadas}",
        f"  encerradas: {resumo.encerradas}",
        f"  inadimplentes distintos (1 mês ou mais): {resumo.inadimplentes}",
        f"  ignoradas Energisa: {resumo.ignoradas_energisa}",
        f"  sem telefone: {resumo.sem_telefone}",
        f"  telefones compartilhados por mais de uma matrícula: {resumo.telefones_compartilhados}",
        f"  linhas inválidas: {resumo.linhas_invalidas}",
        f"  linhas de outra data: {resumo.data_ref_divergente}",
        f"  mantidas por Pix pendente: {resumo.preservadas}",
        f"  total do retrato: {resumo.total_retrato}",
    ]
    return linhas


def _fase_importar(ctx: _Contexto) -> _Fase:
    esperado = (
        f"custo esperado: ~{CHAMADAS_ESPERADAS_DO_RETRATO} chamadas, "
        f"~{LINHAS_ESPERADAS_DO_RETRATO} linhas"
    )
    ctx.progresso(f"importar: retrato de {ctx.data_ref.isoformat()}; {esperado}")
    linhas = [f"  retrato de {ctx.data_ref.isoformat()}; {esperado}"]
    medidor = ctx.medidor
    resta_chamadas = medidor.teto_chamadas - medidor.chamadas
    resta_linhas = medidor.teto_linhas - medidor.linhas
    if resta_chamadas < CHAMADAS_ESPERADAS_DO_RETRATO or resta_linhas < LINHAS_ESPERADAS_DO_RETRATO:
        linhas.append(
            f"  NÃO INICIADA: restam {resta_chamadas} chamadas e {resta_linhas} linhas de cota, "
            "menos que o custo esperado. Suba o teto (--teto-chamadas, --teto-linhas)."
        )
        return _Fase(linhas, ok=False)
    antes = _marca(ctx)
    with ctx.fabrica() as sessao:
        resumo = importar_do_ctn(
            sessao, ctx.ctn, ctx.politica, ctx.data_ref, ctx.campanha, False, ctx.agora()
        )
    linhas += _linhas_do_resumo(resumo)
    ok = True
    if medidor.estourou:
        linhas.append(f"  ABORTADA: {medidor.estourou}; nada foi gravado nem encerrado.")
        ok = False
    elif resumo.motivo_sem_encerrar == SEM_ENCERRAR_INCOMPLETO:
        linhas.append("  RETRATO INCOMPLETO: o CTN falhou na leitura; nada foi gravado.")
        ok = False
    elif resumo.motivo_sem_encerrar is not None:
        linhas.append(
            f"  motivo de não encerrar: {resumo.motivo_sem_encerrar} (pede revisão antes de seguir)"
        )
        ok = False
    linhas.append(_custo(ctx, antes))
    return _Fase(linhas, ok)


# ---- fase 2: conferir ----


def _resultado_da_conferencia(conferencia) -> str:
    if conferencia.conferida:
        return "quitada" if conferencia.quitada_no_ctn else "conferida"
    return conferencia.motivo or "ctn_fora"


def _fase_conferir(ctx: _Contexto) -> _Fase:
    antes = _marca(ctx)
    resultados: Counter = Counter()
    meses: Counter = Counter()
    propensas = 0
    feitas = 0
    with ctx.fabrica() as sessao:
        ids = list(
            sessao.scalars(
                select(Cobranca.id)
                .where(Cobranca.status == StatusCobranca.ATIVA)
                .order_by(Cobranca.id)
                .limit(ctx.conferir)
            )
        )
        if not ids:
            return _Fase(["  nenhuma cobrança ATIVA no banco (rode a fase importar antes)"], False)
        ctx.progresso(f"conferir: {len(ids)} cobranças ATIVAS (2 chamadas cada)")
        for cobranca_id in ids:
            try:
                conferencia = conferir_cobranca(
                    sessao, ctx.ctn, ctx.politica, cobranca_id, ctx.agora()
                )
                sessao.commit()
            except Exception as erro:
                sessao.rollback()
                resultados[f"erro_{type(erro).__name__}"] += 1
                continue
            if ctx.medidor.estourou:
                resultados["interrompida_pelo_teto"] += 1
                break
            feitas += 1
            resultados[_resultado_da_conferencia(conferencia)] += 1
            cobranca = sessao.get(Cobranca, cobranca_id)
            if conferencia.conferida and cobranca.mensalidades_retrato is not None:
                no_ctn = len(conferencia.meses_em_aberto)
                if no_ctn == cobranca.mensalidades_retrato:
                    meses["iguais"] += 1
                elif no_ctn < cobranca.mensalidades_retrato:
                    meses["menores"] += 1
                else:
                    meses["maiores"] += 1
                propensas += 1 if conferencia.propenso else 0
    linhas = [
        f"  cobranças ATIVAS pedidas: {len(ids)}; conferidas até o fim: {feitas}",
        f"  resultado: {_contagem(resultados)}",
        f"  meses em aberto no CTN contra o retrato: {_contagem(meses)}",
        f"  propensas ao cartão entre as conferidas: {propensas}",
    ]
    ok = True
    if ctx.medidor.estourou:
        linhas.append(f"  ABORTADA: {ctx.medidor.estourou}")
        ok = False
    linhas.append(_custo(ctx, antes))
    return _Fase(linhas, ok)


# ---- fases 3 e 4: baixas e ciclos ----


def _ultima_cobranca(sessao, matricula: str) -> Cobranca | None:
    return sessao.scalars(
        select(Cobranca)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(Cliente.matricula_ctn == matricula)
        .order_by(Cobranca.id.desc())
    ).first()


def _linha_de_exemplo(sessao, registro: BaixaCTNRegistro) -> str:
    cobranca = (
        sessao.get(Cobranca, registro.cobranca_id)
        if registro.cobranca_id is not None
        else _ultima_cobranca(sessao, registro.matricula)
    )
    efeito = registro.efeito or "por_tratar"
    if cobranca is None:
        return f"  {mascarar(registro.matricula)} -> {efeito} -> sem cobrança"
    motivo = cobranca.encerrada_motivo or "-"
    texto = (
        f"  {mascarar(registro.matricula)} -> {efeito} -> cobrança {cobranca.status.value}"
        f" -> motivo {motivo}"
    )
    if cobranca.mensalidades_retrato is not None:
        texto += (
            f"; meses em atraso: retrato {cobranca.mensalidades_retrato},"
            f" agora {cobranca.mensalidades_abertas}"
        )
    return texto


def _passada(ctx: _Contexto, visto_em: datetime) -> _Passada:
    """Uma passada de `ler_baixas` e o que ela gravou de novo (sem dado de cliente)."""
    antes = _marca(ctx)
    with ctx.fabrica() as sessao:
        maior_id = sessao.scalar(select(func.max(BaixaCTNRegistro.id))) or 0
        resumo = ler_baixas(sessao, ctx.deps, visto_em)
        com_cobranca = set(
            sessao.scalars(
                select(Cliente.matricula_ctn).join(Cobranca, Cobranca.cliente_id == Cliente.id)
            )
        )
        registros = list(
            sessao.scalars(
                select(BaixaCTNRegistro)
                .where(BaixaCTNRegistro.id > maior_id)
                .order_by(BaixaCTNRegistro.coletado_em, BaixaCTNRegistro.id)
            )
        )
        novas = [
            _Nova(_como_utc(r.coletado_em), r.efeito, r.matricula in com_cobranca)
            for r in registros
        ]
        casadas = [r for r in registros if r.matricula in com_cobranca]
        casadas.sort(key=lambda r: 0 if _de_interesse(r.efeito) else 1)
        exemplos = [_linha_de_exemplo(sessao, r) for r in casadas[:EXEMPLOS_DE_BAIXA]]
    return _Passada(
        visto_em,
        resumo,
        novas,
        exemplos,
        ctx.medidor.chamadas - antes[0],
        ctx.medidor.linhas - antes[1],
    )


def _efeitos(novas: Sequence[_Nova]) -> Counter:
    return Counter(nova.efeito or "por_tratar" for nova in novas)


def _linhas_do_resumo_de_baixas(resumo: ResumoBaixas) -> str:
    return (
        f"lidas={resumo.lidas}, aplicadas={resumo.aplicadas}, quitadas={resumo.quitadas},"
        f" abatidas={resumo.abatidas}, ignoradas={resumo.ignoradas}, puladas={resumo.puladas}"
    )


def _fase_baixas(ctx: _Contexto) -> _Fase:
    ctx.progresso("baixas: uma passada de ler_baixas com o CTN real")
    passada = _passada(ctx, ctx.agora())
    casadas = sum(1 for nova in passada.novas if nova.casada)
    linhas = [
        f"  baixas novas gravadas: {len(passada.novas)}"
        f" (casadas com cobrança do banco local: {casadas};"
        f" sem cobrança: {len(passada.novas) - casadas})",
        f"  efeitos das novas: {_contagem(_efeitos(passada.novas))}",
        f"  resumo da rotina: {_linhas_do_resumo_de_baixas(passada.resumo)}",
        "  exemplos (até 5 casadas, quitou/abateu primeiro):",
        *(passada.exemplos or ["  (nenhuma baixa casada com cobrança)"]),
        f"  custo da fase: {passada.chamadas} chamadas, {passada.linhas} linhas",
    ]
    ok = True
    if ctx.medidor.estourou:
        linhas.append(f"  ABORTADA: {ctx.medidor.estourou}")
        ok = False
    return _Fase(linhas, ok)


def _fase_ciclos(ctx: _Contexto) -> _Fase:
    inicio = ctx.agora()
    baseline: list[float] = []
    depois: list[float] = []
    adiantadas = 0
    efeitos: Counter = Counter()
    linhas: list[str] = []
    feitos = 0
    ok = True
    try:
        for ciclo in range(1, ctx.ciclos + 1):
            visto_em = ctx.agora()
            passada = _passada(ctx, visto_em)
            feitos = ciclo
            atrasos = [atraso_em_minutos(visto_em, nova.coletado_em) for nova in passada.novas]
            adiantadas += sum(1 for atraso in atrasos if atraso < 0)
            validos = [atraso for atraso in atrasos if atraso >= 0]
            (baseline if ciclo == 1 else depois).extend(validos)
            efeitos.update(_efeitos(passada.novas))
            papel = " (carga inicial)" if ciclo == 1 else ""
            linhas.append(
                f"  ciclo {ciclo}/{ctx.ciclos}{papel}: novas={len(passada.novas)},"
                f" {_linhas_do_resumo_de_baixas(passada.resumo)},"
                f" custo {passada.chamadas} chamadas e {passada.linhas} linhas"
            )
            ctx.progresso(linhas[-1].strip())
            if ctx.medidor.estourou:
                linhas.append(f"  ABORTADA: {ctx.medidor.estourou}")
                ok = False
                break
            if ciclo < ctx.ciclos:
                alvo = inicio + timedelta(minutes=ciclo * ctx.intervalo_min)
                ctx.dormir(max(0.0, (alvo - ctx.agora()).total_seconds()))
    except KeyboardInterrupt:
        linhas.append("  INTERROMPIDO pelo operador; o relatório é parcial")
        ok = False
    resumo = [
        f"  ciclos feitos: {feitos} de {ctx.ciclos}, a cada {ctx.intervalo_min:g} min",
        f"  atraso entre coletado_em e o ciclo ver a baixa, ciclos 2 em diante"
        f" (decide a MARGEM_DE_RELEITURA, hoje {MARGEM_DE_RELEITURA.total_seconds() / 60:g} min):"
        f" {formatar_atraso(depois)}",
        f"  atraso do ciclo 1 (carga inicial; NÃO entra na decisão): {formatar_atraso(baseline)}",
        f"  baixas com coletado_em no futuro (fora das contas): {adiantadas}",
        f"  efeitos de todas as novas: {_contagem(efeitos)}",
        "  limite: só mede a baixa com coletado_em >= cursor; a que a API mostrar tarde com"
        " coletado_em antigo não aparece aqui",
    ]
    return _Fase(resumo + linhas, ok)


_FASE_DE = {
    "importar": _fase_importar,
    "conferir": _fase_conferir,
    "baixas": _fase_baixas,
    "ciclos": _fase_ciclos,
}


# ---- a execução ----


def _numeros_do_uso(uso) -> dict:
    return {k: v for k, v in (uso or {}).items() if isinstance(v, int) and not isinstance(v, bool)}


def executar(
    chave: str,
    banco: Path,
    *,
    fases: Sequence[str] = FASES_PADRAO,
    url: str = URL_EFICAZ,
    unidade: str = UNIDADE_PADRAO,
    politica_path: Path = RAIZ_DO_REPO / "politica.toml",
    campanha: str = CAMPANHA,
    data_ref: date | None = None,
    conferir: int = COBRANCAS_A_CONFERIR,
    ciclos: int = CICLOS,
    intervalo_min: float = INTERVALO_EM_MINUTOS,
    teto_linhas: int = TETO_DE_LINHAS,
    teto_chamadas: int = TETO_DE_CHAMADAS,
    relatorio: Path | None = None,
    raiz_do_repo: Path = RAIZ_DO_REPO,
    transporte: httpx.BaseTransport | None = None,
    agora: Callable[[], datetime] = lambda: datetime.now(UTC),
    dormir: Callable[[float], None] = time.sleep,
    progresso: Callable[[str], None] = lambda _mensagem: None,
) -> Resultado:
    """Roda as fases e devolve o relatório. `transporte`, `agora` e `dormir` servem aos testes."""
    banco = validar_caminho(banco, raiz_do_repo)
    if relatorio is not None:
        relatorio = validar_caminho(relatorio, raiz_do_repo, exigir_pre_sombra=False)
    desconhecidas = [fase for fase in fases if fase not in FASES]
    if desconhecidas:
        raise ErroDaConferencia(f"fase desconhecida: {', '.join(desconhecidas)}")
    a_rodar = [fase for fase in FASES if fase in fases]
    politica = carregar_politica(politica_path)

    banco.parent.mkdir(parents=True, exist_ok=True)
    url_do_banco = f"sqlite:///{banco.as_posix()}"
    migrar(url_do_banco)
    # ocultar_parametros: o SQL de um erro não pode levar nome nem telefone para a tela.
    fabrica = criar_sessao(url_do_banco, ocultar_parametros=True)

    medidor = Medidor(transporte or httpx.HTTPTransport(), teto_chamadas, teto_linhas)
    chatwoot = ChatwootSemEnvio()
    inicio = agora()
    hoje = inicio.astimezone(politica.janela.zona).date()
    secoes: list[str] = []
    todas_ok = True
    interrompido = False
    try:
        with CTNEficaz(url, chave, unidade, transporte=medidor, agora=agora) as ctn:
            ctx = _Contexto(
                fabrica=fabrica,
                ctn=ctn,
                medidor=medidor,
                politica=politica,
                deps=DependenciasDasBaixas(
                    politica=politica,
                    chatwoot=chatwoot,
                    time_humano=None,
                    modo=Modo.SOMBRA,
                    ctn=ctn,
                ),
                agora=agora,
                dormir=dormir,
                progresso=progresso,
                campanha=campanha,
                data_ref=data_ref or hoje,
                conferir=conferir,
                ciclos=ciclos,
                intervalo_min=intervalo_min,
            )
            for fase in a_rodar:
                if medidor.estourou:
                    secoes.append(
                        f"== {fase} ==\n  NÃO RODADA: o teto de cota já foi atingido"
                        f" ({medidor.estourou})"
                    )
                    todas_ok = False
                    continue
                try:
                    resultado = _FASE_DE[fase](ctx)
                except KeyboardInterrupt:
                    secoes.append(f"== {fase} ==\n  INTERROMPIDA pelo operador")
                    todas_ok = False
                    interrompido = True
                    break
                except ErroDaConferencia as erro:
                    secoes.append(f"== {fase} ==\n  FALHOU: {erro}")
                    todas_ok = False
                    continue
                except Exception as erro:
                    # Só o tipo: a mensagem de um erro de banco ou de rede pode levar dado.
                    secoes.append(f"== {fase} ==\n  FALHOU ({type(erro).__name__})")
                    todas_ok = False
                    continue
                todas_ok = todas_ok and resultado.ok
                secoes.append(f"== {fase} ==\n" + "\n".join(resultado.linhas))
            uso = _numeros_do_uso(ctn.ultimo_uso)
    finally:
        fabrica.kw["bind"].dispose()

    cota = [
        "== Cota ==",
        f"  chamadas: {medidor.chamadas} de no máximo {teto_chamadas}",
        f"  linhas devolvidas: {medidor.linhas} de no máximo {teto_linhas}"
        f" (por rota: {_contagem(medidor.linhas_por_rota)})",
        f"  `uso` da API (último corpo lido): {_contagem(Counter(uso))}",
        "  cota da chave por dia: 5.000 chamadas e 20.000 linhas",
    ]
    falso = [
        "== Chatwoot falso (nada saiu) ==",
        f"  avisos de baixa que sairiam: {chatwoot.avisos}; notas ao atendente: {chatwoot.notas}",
    ]
    if interrompido:
        veredito = "RESULTADO: INTERRUPÇÃO do operador; relatório parcial"
    elif medidor.estourou:
        veredito = f"RESULTADO: ABORTADO por cota ({medidor.estourou}); relatório parcial"
    elif todas_ok:
        veredito = "RESULTADO: concluído"
    else:
        veredito = "RESULTADO: concluído com ressalvas (veja as fases)"
    cabecalho = (
        f"Ensaio em sombra do CTN real (unidade {unidade}), "
        f"{inicio.astimezone(FUSO_DO_NEGOCIO):%Y-%m-%d %H:%M} em Brasília\n"
        f"fases: {', '.join(a_rodar)}; banco: {banco}; só leitura no CTN; nada é enviado"
    )
    texto = "\n\n".join([cabecalho, *secoes, "\n".join(cota), "\n".join(falso), veredito])
    if relatorio is not None:
        relatorio.parent.mkdir(parents=True, exist_ok=True)
        relatorio.write_text(texto + "\n", encoding="utf-8")
    return Resultado(texto, 0 if todas_ok else 1, chatwoot, medidor)


# ---- a linha de comando ----


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("use AAAA-MM-DD") from None


def _positivo(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("use um inteiro") from None
    if valor < 1:
        raise argparse.ArgumentTypeError("use um inteiro de 1 em diante")
    return valor


def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description="Ensaio em sombra do CTN real (PARADA 3).")
    analisador.add_argument("--banco", type=Path, required=True, help="SQLite em pre-sombra/")
    analisador.add_argument(
        "--fase",
        action="append",
        choices=FASES,
        help=f"repetível; padrão: {', '.join(FASES_PADRAO)}",
    )
    analisador.add_argument("--conferir", type=_positivo, default=COBRANCAS_A_CONFERIR)
    analisador.add_argument("--ciclos", type=_positivo, default=CICLOS)
    analisador.add_argument("--intervalo-min", type=float, default=INTERVALO_EM_MINUTOS)
    analisador.add_argument("--teto-linhas", type=_positivo, default=TETO_DE_LINHAS)
    analisador.add_argument("--teto-chamadas", type=_positivo, default=TETO_DE_CHAMADAS)
    analisador.add_argument("--relatorio", type=Path, help="texto com as mesmas contagens")
    analisador.add_argument("--data", type=_data, help="AAAA-MM-DD do retrato; padrão hoje")
    analisador.add_argument("--campanha", default=CAMPANHA)
    analisador.add_argument("--url", default=URL_EFICAZ)
    analisador.add_argument("--unidade", default=UNIDADE_PADRAO)
    analisador.add_argument("--politica", type=Path, default=RAIZ_DO_REPO / "politica.toml")
    analisador.add_argument("--segredos", type=Path, default=WORKSPACE / "secrets")
    return analisador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    # O console do Windows pode não ser UTF-8; o relatório tem acento.
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8")
    argumentos = _argumentos(argv)
    if argumentos.intervalo_min <= 0:
        print("erro: --intervalo-min precisa ser maior que 0", file=sys.stderr)
        return 2
    try:
        # O caminho antes da chave: recusa cedo, sem abrir nada.
        validar_caminho(argumentos.banco)
        chave = ler_chave(os.environ, argumentos.segredos)
        resultado = executar(
            chave,
            argumentos.banco,
            fases=argumentos.fase or FASES_PADRAO,
            url=argumentos.url,
            unidade=argumentos.unidade,
            politica_path=argumentos.politica,
            campanha=argumentos.campanha,
            data_ref=argumentos.data,
            conferir=argumentos.conferir,
            ciclos=argumentos.ciclos,
            intervalo_min=argumentos.intervalo_min,
            teto_linhas=argumentos.teto_linhas,
            teto_chamadas=argumentos.teto_chamadas,
            relatorio=argumentos.relatorio,
            progresso=lambda mensagem: print(f"... {mensagem}", flush=True),
        )
    except ErroDaConferencia as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    print(resultado.texto)
    return resultado.codigo


if __name__ == "__main__":
    sys.exit(main())
