"""Conferência de leitura do CTN real (PARADA 1; plano `specs/planos/2026-11-06-integracao-ctn.md`).

Roda `consultar_cliente`, `historico_pagamentos` e `mensalidades_em_aberto` de 3 matrículas reais e
sonda a API para confirmar os formatos que a spec assume. Imprime SÓ FORMATOS E CONTAGENS: nenhum
telefone, nome, CPF, valor de cliente nem a chave. A matrícula sai mascarada (últimos 3 dígitos).

Uso, de dentro de repo/:
    python -m uv run python ferramentas/ctn/conferir_leitura.py
    python -m uv run python ferramentas/ctn/conferir_leitura.py --matricula A --matricula B

A chave vem da variável `AGENTE_CTN_API_KEY` (tem prioridade) ou do campo `token` de
`<segredos>/eficaz.local.json`. URL e unidade seguem os padrões do `CTNEficaz`.

Sem `--matricula`, pega as 3 primeiras do retrato diário (`--data-ref`, padrão ontem em Brasília)
com `meses_atraso >= 1`. Gasta menos de 40 chamadas (`--orcamento`) e respeita o limite de 55 por
minuto do próprio `CTNEficaz`. Nenhuma varredura funda: cada consulta de diagnóstico é 1 página.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from agente.integracoes.ctn import ClienteNaoEncontrado, CTNIndisponivel
from agente.integracoes.ctn_eficaz import (
    FUSO_DO_NEGOCIO,
    ROTA_BAIXAS,
    ROTA_FILIADOS,
    ROTA_INADIMPLENCIA,
    ROTA_LANCAMENTOS,
    STATUS_REJEITADA,
    UNIDADE_PADRAO,
    URL_EFICAZ,
    CTNEficaz,
)

WORKSPACE = Path(__file__).resolve().parents[3]
VARIAVEL_DA_CHAVE = "AGENTE_CTN_API_KEY"
ARQUIVO_DA_CHAVE = "eficaz.local.json"
CAMPO_DA_CHAVE = "token"

TOTAL_DE_MATRICULAS = 3
MESES_DO_HISTORICO = 12
DIAS_DE_BAIXAS = 3
LINHAS_POR_DIA = 500
ORCAMENTO_DE_CHAMADAS = 39
PRAZO_DA_SONDA = 12.0

# O formato dos campos que a spec assume; os valores nunca são impressos, só o molde.
CAMPOS_DE_FORMATO: dict[str, tuple[str, ...]] = {
    ROTA_INADIMPLENCIA: ("telefone", "ult_mesref", "data_ref", "meses_atraso", "meses_ate_corte"),
    ROTA_FILIADOS: ("telefone",),
    ROTA_LANCAMENTOS: ("mes_ano", "valor", "data_baixa"),
    ROTA_BAIXAS: ("mes_ref", "dt_baixa", "dia_relatorio", "coletado_em", "valor"),
}
# O catálogo de formas de pagamento não é dado de cliente: sai o texto bruto, para conferir o mapa.
CAMPO_DA_FORMA = "forma"
_MOLDE_DO_MES = re.compile(r"\d{4}/\d{2}")


class ErroDaConferencia(Exception):
    """Falha que o operador resolve sem olhar o código. Não carrega valor de cliente nem a chave."""


# ---- o que a resposta bruta mostra, sem guardar valor de cliente ----


def molde(valor: object) -> str:
    """O formato de um valor, nunca o valor: texto vira o molde com cada dígito como 9."""
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "bool"
    if isinstance(valor, int):
        return "int"
    if isinstance(valor, float):
        return "float"
    if isinstance(valor, str):
        return re.sub(r"\d", "9", valor)[:40]
    return type(valor).__name__


def _tipo_json(valor: object) -> str:
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "boolean"
    if isinstance(valor, int | float):
        return "number"
    if isinstance(valor, str):
        return "string"
    if isinstance(valor, list):
        return "array"
    return "object"


class Gravador(httpx.BaseTransport):
    """Embrulha o transporte e resume cada resposta: chaves do topo, tipo do `mais`, moldes dos
    campos, `data_baixa` das rejeitadas. Guarda só moldes e contagens. Também conta as chamadas e
    recusa a que passar do orçamento."""

    def __init__(self, interno: httpx.BaseTransport, orcamento: int):
        self._interno = interno
        self.orcamento = orcamento
        self.chamadas = 0
        self.status: dict[str, Counter] = {}
        self.chaves_do_topo: dict[str, set[str]] = {}
        self.chaves_da_linha: dict[str, set[str]] = {}
        self.tipos_do_mais: dict[str, set[str]] = {}
        self.formatos: dict[tuple[str, str], Counter] = {}
        self.formas_brutas: Counter = Counter()
        self.data_baixa_das_rejeitadas: Counter = Counter()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.chamadas >= self.orcamento:
            raise ErroDaConferencia(f"orçamento de {self.orcamento} chamadas esgotado")
        self.chamadas += 1
        resposta = self._interno.handle_request(request)
        resposta.read()
        conteudo = resposta.content
        resposta.close()
        rota = request.url.path
        self.status.setdefault(rota, Counter())[resposta.status_code] += 1
        if resposta.status_code == 200:
            self._registrar(rota, conteudo)
        # Uma resposta nova, já decodificada, para o cliente não decodificar duas vezes.
        cabecalhos = [
            (k, v)
            for k, v in resposta.headers.items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            resposta.status_code, headers=cabecalhos, content=conteudo, request=request
        )

    def _registrar(self, rota: str, conteudo: bytes) -> None:
        try:
            corpo = json.loads(conteudo)
        except ValueError:
            return
        if not isinstance(corpo, Mapping):
            return
        self.chaves_do_topo.setdefault(rota, set()).update(str(k) for k in corpo)
        self.tipos_do_mais.setdefault(rota, set()).add(
            _tipo_json(corpo["mais"]) if "mais" in corpo else "ausente"
        )
        dados = corpo.get("dados")
        if not isinstance(dados, list):
            return
        for linha in dados:
            if not isinstance(linha, Mapping):
                continue
            self.chaves_da_linha.setdefault(rota, set()).update(str(k) for k in linha)
            for campo in CAMPOS_DE_FORMATO.get(rota, ()):
                formato = molde(linha[campo]) if campo in linha else "ausente"
                self.formatos.setdefault((rota, campo), Counter())[formato] += 1
            if rota == ROTA_BAIXAS:
                bruta = linha.get(CAMPO_DA_FORMA)
                self.formas_brutas[str(bruta)[:40] if bruta is not None else "null"] += 1
            if rota == ROTA_LANCAMENTOS and linha.get("status_lancamento") == STATUS_REJEITADA:
                if "data_baixa" not in linha:
                    self.data_baixa_das_rejeitadas["ausente"] += 1
                elif linha["data_baixa"] is None:
                    self.data_baixa_das_rejeitadas["nula"] += 1
                else:
                    self.data_baixa_das_rejeitadas["preenchida"] += 1


def mascarar(matricula: str) -> str:
    return "***" + matricula[-3:] if len(matricula) > 3 else "***"


def ler_chave(ambiente: Mapping[str, str], segredos: Path) -> str:
    """A variável tem prioridade; senão o campo `token` do JSON. Nenhuma mensagem leva o valor."""
    da_variavel = ambiente.get(VARIAVEL_DA_CHAVE, "").strip()
    if da_variavel:
        return da_variavel
    arquivo = segredos / ARQUIVO_DA_CHAVE
    if not arquivo.is_file():
        raise ErroDaConferencia(f"sem chave: defina {VARIAVEL_DA_CHAVE} ou crie {arquivo.name}")
    try:
        conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ErroDaConferencia(f"{arquivo.name} não é um JSON legível") from None
    chave = conteudo.get(CAMPO_DA_CHAVE) if isinstance(conteudo, Mapping) else None
    if not isinstance(chave, str) or not chave.strip():
        raise ErroDaConferencia(f"{arquivo.name} não tem o campo '{CAMPO_DA_CHAVE}'")
    return chave.strip()


# ---- as consultas ----


def _falha(erro: Exception) -> str:
    """A mensagem de CTNIndisponivel já vem sem URL, corpo e chave; o resto só dá o tipo."""
    if isinstance(erro, CTNIndisponivel):
        return f"falhou ({erro})"
    return f"falhou ({type(erro).__name__})"


def _ordens(valores: Sequence[int]) -> str:
    """A faixa em ordem de grandeza (10^k centavos), nunca o valor."""
    if not valores:
        return "sem valores"
    baixo, alto = min(valores), max(valores)
    inteiros = all(isinstance(v, int) and v > 0 for v in valores)
    faixa = f"10^{len(str(baixo)) - 1}..10^{len(str(alto)) - 1} centavos"
    return f"{faixa}, inteiros positivos: {'sim' if inteiros else 'NAO'}"


def _distribuicao(contagem: Mapping[object, int]) -> str:
    texto = ", ".join(f"{k}={v}" for k, v in sorted(contagem.items(), key=lambda x: str(x[0])))
    return texto or "-"


def conferir_matricula(
    ctn: CTNEficaz, matricula: str, meses: int, atraso: tuple[int, int] | None
) -> list[str]:
    linhas = [f"matrícula {mascarar(matricula)}"]
    if atraso is None:
        linhas.append("  retrato: meses_atraso/meses_ate_corte n/d (matrícula passada à mão)")
    else:
        linhas.append(f"  retrato: meses_atraso={atraso[0]}, meses_ate_corte={atraso[1]}")
    try:
        cliente = ctn.consultar_cliente(matricula)
        telefone = cliente.telefone
        com_9 = len(telefone) == 14 and telefone.startswith("+55") and telefone[5] == "9"
        linhas.append(
            f"  consultar_cliente: telefone E.164 {'com o 9' if com_9 else 'SEM o 9 ou vazio'}"
            f" (tamanho {len(telefone)}, começa com +55: {telefone.startswith('+55')}),"
            f" tem nome: {bool(cliente.nome.strip())}, tem CPF: {bool(cliente.cpf)}"
        )
    except ClienteNaoEncontrado:
        linhas.append("  consultar_cliente: matrícula não encontrada")
    except ErroDaConferencia:
        raise
    except Exception as erro:
        linhas.append(f"  consultar_cliente: {_falha(erro)}")
    try:
        historico = ctn.historico_pagamentos(matricula, meses)
        formas = Counter(p.forma.value for p in historico)
        linhas.append(
            f"  historico_pagamentos({meses} meses): {len(historico)} baixas;"
            f" por forma: {_distribuicao(formas) or 'nenhuma'}; valor: "
            + _ordens([p.valor_centavos for p in historico])
        )
    except ErroDaConferencia:
        raise
    except Exception as erro:
        linhas.append(f"  historico_pagamentos: {_falha(erro)}")
    try:
        abertas = ctn.mensalidades_em_aberto(matricula)
        formato_ok = sum(1 for m in abertas if _MOLDE_DO_MES.fullmatch(m.mes_ano))
        linhas.append(
            f"  mensalidades_em_aberto: {len(abertas)} em aberto;"
            f" mes_ano no padrão AAAA/MM: {formato_ok} de {len(abertas)}; valor: "
            + _ordens([m.valor_centavos for m in abertas])
        )
    except ErroDaConferencia:
        raise
    except Exception as erro:
        linhas.append(f"  mensalidades_em_aberto: {_falha(erro)}")
    return linhas


def escolher_matriculas(
    ctn: CTNEficaz, data_ref: date, total: int = TOTAL_DE_MATRICULAS
) -> list[tuple[str, tuple[int, int]]]:
    """As primeiras `total` do retrato com `meses_atraso >= 1`; para de ler ao achá-las."""
    achadas = []
    for inadimplente in ctn.inadimplentes(data_ref):
        if inadimplente.meses_atraso >= 1:
            achadas.append(
                (inadimplente.matricula, (inadimplente.meses_atraso, inadimplente.meses_ate_corte))
            )
            if len(achadas) == total:
                break
    return achadas


class Sonda:
    """Consultas de diagnóstico que o `CTNEficaz` não expõe, uma página por chamada. Usa o mesmo
    transporte (que conta e resume) e a vaga do limite de taxa do cliente."""

    def __init__(self, ctn: CTNEficaz, url: str, chave: str, transporte: httpx.BaseTransport):
        self._ctn = ctn
        self._cliente = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {chave}", "Accept": "application/json"},
            timeout=PRAZO_DA_SONDA,
            transport=transporte,
        )

    def fechar(self) -> None:
        self._cliente.close()

    def obter(self, rota: str, params: Mapping[str, object]) -> tuple[int | None, object]:
        """(status, corpo JSON ou None). Falha de rede: (None, None)."""
        self._ctn._esperar_vaga()
        try:
            resposta = self._cliente.get(rota, params=dict(params))
        except httpx.HTTPError:
            return None, None
        try:
            return resposta.status_code, resposta.json()
        except ValueError:
            return resposta.status_code, None

    def status_com_e_sem_ordem(self, matricula: str) -> list[tuple[str, str, int | None]]:
        base = {"unidade": self._ctn.unidade, "pagina": 0, "limite": 1}
        filtros = {
            ROTA_LANCAMENTOS: {"matricula_titular": matricula, "status_lancamento": "rejeitada"},
            ROTA_BAIXAS: {"matricula": matricula},
        }
        resultado = []
        for rota, filtro in filtros.items():
            for rotulo, ordem in (
                ("ordem=id", {"ordem": "id", "direcao": "asc"}),
                ("sem ordem", {}),
            ):
                status, _ = self.obter(rota, {**filtro, **base, **ordem})
                resultado.append((rota, rotulo, status))
        return resultado

    def baixas_do_dia(self, dia: date, limite: int) -> dict:
        """Uma página das baixas de `de=ate=dia` e onde cada coluna de data cai em relação a ele."""
        texto = dia.isoformat()
        params = {
            "unidade": self._ctn.unidade,
            "de": texto,
            "ate": texto,
            "ordem": "id",
            "direcao": "asc",
            "pagina": 0,
            "limite": limite,
        }
        status, corpo = self.obter(ROTA_BAIXAS, params)
        resultado: dict = {"dia": texto, "status": status, "linhas": None, "mais": None}
        dados = corpo.get("dados") if isinstance(corpo, Mapping) else None
        if status != 200 or not isinstance(dados, list):
            return resultado
        resultado["linhas"] = len(dados)
        resultado["mais"] = corpo.get("mais")
        for coluna in ("dia_relatorio", "dt_baixa"):
            contagem = Counter()
            for linha in dados:
                valor = linha.get(coluna) if isinstance(linha, Mapping) else None
                if not isinstance(valor, str) or not valor:
                    contagem["ausentes"] += 1
                elif valor[:10] == texto:
                    contagem["dentro"] += 1
                else:
                    contagem["fora"] += 1
            resultado[coluna] = contagem
        return resultado


# ---- o relatório ----


def _linha_do_mais(gravador: Gravador) -> list[str]:
    linhas = []
    for rota in sorted(gravador.tipos_do_mais):
        linhas.append(
            f"  {rota}: `mais` é {', '.join(sorted(gravador.tipos_do_mais[rota]))}; "
            f"chaves do corpo: {', '.join(sorted(gravador.chaves_do_topo.get(rota, ())))}"
        )
    return linhas or ["  nenhuma resposta 200 lida"]


def executar(
    chave: str,
    *,
    url: str = URL_EFICAZ,
    unidade: str = UNIDADE_PADRAO,
    matriculas: Sequence[str] = (),
    data_ref: date | None = None,
    meses: int = MESES_DO_HISTORICO,
    limite_do_dia: int = LINHAS_POR_DIA,
    orcamento: int = ORCAMENTO_DE_CHAMADAS,
    transporte: httpx.BaseTransport | None = None,
    agora: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Roda a conferência e devolve o relatório em texto. `transporte` serve aos testes."""
    hoje = agora().astimezone(FUSO_DO_NEGOCIO).date()
    data_ref = data_ref or hoje - timedelta(days=1)
    gravador = Gravador(transporte or httpx.HTTPTransport(), orcamento)
    secoes: list[str] = []
    interrompido = None
    por_matricula: list[str] = []
    status_das_rotas: list[str] = []
    baixas: list[dict] = []
    origem = "nenhuma"
    with CTNEficaz(url, chave, unidade, transporte=gravador, agora=agora) as ctn:
        sonda = Sonda(ctn, url, chave, gravador)
        try:
            if matriculas:
                alvo = [(m, None) for m in matriculas]
                origem = "passadas com --matricula"
            else:
                try:
                    alvo = escolher_matriculas(ctn, data_ref)
                except ErroDaConferencia:
                    raise
                except Exception as erro:
                    alvo = []
                    por_matricula.append(f"retrato de {data_ref}: {_falha(erro)}")
                origem = f"{len(alvo)} primeiras do retrato de {data_ref} com meses_atraso >= 1"
            for matricula, atraso in alvo:
                por_matricula += conferir_matricula(ctn, matricula, meses, atraso)
            if alvo:
                for rota, rotulo, status in sonda.status_com_e_sem_ordem(alvo[0][0]):
                    status_das_rotas.append(f"  {rota} {rotulo}: HTTP {status or 'sem resposta'}")
            else:
                status_das_rotas.append("  sem matrícula para sondar")
            for n in range(1, DIAS_DE_BAIXAS + 1):
                baixas.append(sonda.baixas_do_dia(hoje - timedelta(days=n), limite_do_dia))
        except ErroDaConferencia as erro:
            interrompido = str(erro)
        finally:
            sonda.fechar()

    secoes.append("== Matrículas (" + origem + ") ==\n" + "\n".join(por_matricula or ["-"]))
    secoes.append("== 1. Paginação: tipo JSON do `mais` ==\n" + "\n".join(_linha_do_mais(gravador)))
    secoes.append(
        "== 2. Status HTTP com e sem `ordem` (limite=1, 1ª matrícula) ==\n"
        + "\n".join(status_das_rotas)
    )
    rejeitadas = gravador.data_baixa_das_rejeitadas
    secoes.append(
        "== 3. `data_baixa` das `rejeitada` do `lancamentos` (inclui a sonda de 1 linha) ==\n  "
        + (_distribuicao(rejeitadas) if rejeitadas else "nenhuma linha rejeitada lida")
    )
    coluna = ["== 4. Coluna que o `de`/`ate` filtra em `lancamentos_bx` (1ª página de cada dia) =="]
    for dia in baixas:
        if dia["linhas"] is None:
            coluna.append(f"  {dia['dia']}: HTTP {dia['status'] or 'sem resposta'}, sem linhas")
            continue
        coluna.append(
            f"  {dia['dia']}: {dia['linhas']} linhas;"
            f" dia_relatorio {_distribuicao(dia['dia_relatorio'])};"
            f" dt_baixa {_distribuicao(dia['dt_baixa'])}"
            " (dentro/fora: os 10 primeiros caracteres contra o dia pedido, sem fuso)"
        )
    secoes.append("\n".join(coluna))
    por_dia = [
        "== 5. Baixas por dia da unidade (1 chamada por dia, limite=" + str(limite_do_dia) + ") =="
    ]
    for dia in baixas:
        if dia["linhas"] is None:
            por_dia.append(f"  {dia['dia']}: indisponível (HTTP {dia['status'] or 'sem resposta'})")
        else:
            truncado = " (há mais: a contagem é um piso)" if dia["mais"] else ""
            por_dia.append(f"  {dia['dia']}: {dia['linhas']} baixas{truncado}")
    secoes.append("\n".join(por_dia))
    formatos = ["== Formatos brutos (molde: cada dígito vira 9) =="]
    for (rota, campo), contagem in sorted(gravador.formatos.items()):
        formatos.append(f"  {rota} {campo}: {_distribuicao(contagem)}")
    if gravador.formas_brutas:
        formatos.append(
            f"  {ROTA_BAIXAS} forma (texto bruto): {_distribuicao(gravador.formas_brutas)}"
        )
    for rota in sorted(gravador.chaves_da_linha):
        formatos.append(f"  colunas de {rota}: {', '.join(sorted(gravador.chaves_da_linha[rota]))}")
    secoes.append("\n".join(formatos))
    uso = ctn.ultimo_uso or {}
    numeros = {k: v for k, v in uso.items() if isinstance(v, int) and not isinstance(v, bool)}
    rodape = f"== Chamadas == {gravador.chamadas} de no máximo {orcamento}; `uso` da API: "
    rodape += _distribuicao(numeros) if numeros else "n/d"
    if interrompido:
        rodape += f"\nINTERROMPIDO: {interrompido}"
    secoes.append(rodape)
    return f"Conferência de leitura do CTN (unidade {unidade}, hoje {hoje})\n\n" + "\n\n".join(
        secoes
    )


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("use AAAA-MM-DD") from None


def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    analisador = argparse.ArgumentParser(description="Conferência de leitura do CTN real.")
    analisador.add_argument("--matricula", action="append", default=[], help="repetível")
    analisador.add_argument("--data-ref", type=_data, help="AAAA-MM-DD; padrão ontem (Brasília)")
    analisador.add_argument("--url", default=URL_EFICAZ)
    analisador.add_argument("--unidade", default=UNIDADE_PADRAO)
    analisador.add_argument("--meses", type=int, default=MESES_DO_HISTORICO)
    analisador.add_argument("--orcamento", type=int, default=ORCAMENTO_DE_CHAMADAS)
    analisador.add_argument("--limite-do-dia", type=int, default=LINHAS_POR_DIA)
    analisador.add_argument("--segredos", type=Path, default=WORKSPACE / "secrets")
    return analisador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    # O console do Windows pode não ser UTF-8; o relatório tem acento.
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8")
    argumentos = _argumentos(argv)
    try:
        chave = ler_chave(os.environ, argumentos.segredos)
        relatorio = executar(
            chave,
            url=argumentos.url,
            unidade=argumentos.unidade,
            matriculas=argumentos.matricula,
            data_ref=argumentos.data_ref,
            meses=argumentos.meses,
            limite_do_dia=argumentos.limite_do_dia,
            orcamento=argumentos.orcamento,
        )
    except ErroDaConferencia as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    print(relatorio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
