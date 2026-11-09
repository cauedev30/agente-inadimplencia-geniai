"""O CTN real pela API EFICAZ, só de leitura (T9; spec `specs/2026-11-06-integracao-ctn.md`).

Implementa os métodos de leitura do `Protocol CTN`: o cliente, o histórico de pagamentos, o retrato
diário da inadimplência, as mensalidades em aberto e as baixas. A escrita (link, cadastro do cartão
e baixa) não existe na API: o `CTNComposto` a delega ao `CTNFalso`.

O cliente cumpre os limites da chave (60 chamadas por minuto, 50 linhas por resposta, 15 s por
consulta, período de até 31 dias, sempre `ordem`). A mensagem de todo erro leva só o código HTTP ou
o tipo da falha, nunca a URL, a chave nem o corpo da resposta: ela vai para o log e para o turno, e
o corpo traz dado de cliente.
"""

import json
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from datetime import time as hora_do_dia
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from agente.dominio.telefone import normalizar_telefone
from agente.integracoes.ctn import (
    BaixaCTN,
    ClienteCTN,
    ClienteNaoEncontrado,
    ConsultaMensalidades,
    CTNIndisponivel,
    InadimplenteCTN,
    MensalidadeAberta,
)
from agente.tipos import FormaHistorico, PagamentoHistorico

log = logging.getLogger(__name__)

URL_EFICAZ = "https://api.eficaz-group.com"
UNIDADE_PADRAO = "jp"

ROTA_FILIADOS = "/v1/ctn/filiados_full"
ROTA_INADIMPLENCIA = "/v1/ctn/inadimplencia"
ROTA_LANCAMENTOS = "/v1/ctn/lancamentos"
ROTA_BAIXAS = "/v1/ctn/lancamentos_bx"

STATUS_REJEITADA = "rejeitada"
# A chave deixa no máximo 500 linhas por resposta; o padrão dela é 50 (spec, seção 2).
LINHAS_POR_PAGINA = 50
LINHAS_MAXIMAS = 500
# O teto de páginas por consulta (por chamada de `_linhas`): bem abaixo da cota diária de 5.000
# chamadas. Passar dele é falha (CTNIndisponivel), nunca um resultado cortado que parece inteiro.
PAGINAS_MAXIMAS_POR_ROTA = 200
# O "hoje" do histórico e da janela das baixas é o dia de Brasília, não o do UTC.
FUSO_DO_NEGOCIO = ZoneInfo("America/Sao_Paulo")
# O período de uma consulta (`de` a `ate`, inclusive) tem no máximo 31 dias.
DIAS_POR_JANELA = 31
# 55 de 60: sobra folga para o relógio do servidor e para a chamada recusada que também conta.
LIMITE_POR_MINUTO = 55
JANELA_DO_LIMITE = 60.0
# As baixas de uma pessoa ficam em cache por pouco tempo: a conferência as usa duas vezes (o
# casamento com os lançamentos e o histórico da propensão) e a cota diária é de 5.000 chamadas.
VALIDADE_DO_CACHE_DE_BAIXAS = 60.0
CACHE_BAIXAS_MAXIMO = 256
# A API corta a consulta em 15 s; o prazo total do cliente fica abaixo disso.
PRAZO_PADRAO = 12.0

MOTIVO_PRAZO = "o CTN passou do prazo"
MOTIVO_FORMATO = "resposta do CTN em formato inesperado"

# "CAIXA ECONOMICA FEDERAL" fica de fora de propósito: não se sabe se é boleto (pergunta ao dev),
# então cai em OUTRO até haver confirmação.
_FORMAS_DE_CARTAO = {"MASTERCARD", "MASTER", "VISA", "ELO", "HIPERCARD", "DISCOVER"}
_MES = re.compile(r"(\d{4})[/-](\d{2})(?!\d)")
_MES_INVERTIDO = re.compile(r"(\d{2})[/-](\d{4})$")
_MILHAR_AMBIGUO = re.compile(r"[+-]?\d+[.,]\d{3}")


class ConfigCTN(Protocol):
    """O que o `da_config` lê (os campos que a tarefa 1.3 cria na `Config`)."""

    ctn_url: str
    ctn_api_key: str | None
    ctn_unidade: str


class CTNRecusou(CTNIndisponivel):
    """O CTN respondeu com erro HTTP. A mensagem é só o código (nunca a URL nem o corpo)."""

    def __init__(self, status: int):
        super().__init__(f"o CTN respondeu {status}")
        self.status = status


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper().strip()


def forma_do_historico(bruta: object) -> FormaHistorico:
    """A `forma` do CTN (spec, seção 2) na `FormaHistorico`. O que não se conhece, e o dinheiro
    "direto no cartão" (decisão do Cauê, seção 3), vira OUTRO."""
    texto = _sem_acento(bruta) if isinstance(bruta, str) else ""
    if "DINHEIRO" in texto:
        return FormaHistorico.OUTRO
    if texto == "PIX":
        return FormaHistorico.PIX
    if texto.startswith("BOLETO"):
        return FormaHistorico.BOLETO
    if "ENERGISA" in texto:
        return FormaHistorico.ENERGIA
    if texto in _FORMAS_DE_CARTAO or "CARTAO" in texto:
        return FormaHistorico.CARTAO
    return FormaHistorico.OUTRO


# ---- a conversão das linhas da API (qualquer falha vira `_invalida`, sem o conteúdo) ----


def _invalida() -> CTNIndisponivel:
    return CTNIndisponivel(MOTIVO_FORMATO)


def _texto(valor: object) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("texto")
    return valor.strip()


def _inteiro(valor: object) -> int:
    if isinstance(valor, bool):
        raise ValueError("inteiro")
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.strip().isdigit():
        return int(valor)
    raise ValueError("inteiro")


def _data(valor: object) -> date:
    return date.fromisoformat(_texto(valor)[:10])


def _data_ou_none(valor: object) -> date | None:
    return None if valor is None else _data(valor)


def _instante(valor: object) -> datetime:
    """Um instante da API. Só a data vale a meia-noite UTC, e sem fuso vale UTC (o fuso dos dados
    não consta da documentação)."""
    texto = _texto(valor)
    if len(texto) == 10:
        return datetime.combine(date.fromisoformat(texto), hora_do_dia(), tzinfo=UTC)
    instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return instante if instante.tzinfo is not None else instante.replace(tzinfo=UTC)


def _mes(valor: object) -> str:
    """O mês como `AAAA/MM`, venha como `AAAA/MM`, `AAAA-MM[-DD]` ou `MM/AAAA`."""
    texto = _texto(valor)
    achado = _MES.match(texto)
    if achado:
        ano, mes = achado.group(1), achado.group(2)
    else:
        invertido = _MES_INVERTIDO.match(texto)
        if not invertido:
            raise ValueError("mês")
        mes, ano = invertido.group(1), invertido.group(2)
    if not 1 <= int(mes) <= 12:
        raise ValueError("mês")
    return f"{ano}/{mes}"


def _centavos(valor: object) -> int:
    """Reais da API (número ou texto) em centavos, sem erro de ponto flutuante."""
    if isinstance(valor, bool) or not isinstance(valor, int | float | str):
        raise ValueError("valor")
    texto = str(valor).strip()
    # "1.234" pode ser mil duzentos e trinta e quatro ou um real e 23 centavos: o texto com um só
    # separador e 3 dígitos depois não se adivinha. (Um número JSON já vem sem essa dúvida.)
    if isinstance(valor, str) and _MILHAR_AMBIGUO.fullmatch(texto):
        raise ValueError("valor")
    reais = Decimal(texto.replace(",", "."))
    if reais < 0:
        raise ValueError("valor")
    return int((reais * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _cpf(valor: object) -> str | None:
    digitos = re.sub(r"\D", "", valor) if isinstance(valor, str) else ""
    return digitos or None


def _converter[T](linha: Mapping, fabrica: Callable[[Mapping], T]) -> T:
    """Converte uma linha; se faltar campo ou vier num formato que não é o da API, levanta
    CTNIndisponivel sem o conteúdo (nem o erro de origem)."""
    try:
        return fabrica(linha)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    raise _invalida()


def _inadimplente(linha: Mapping) -> InadimplenteCTN:
    return InadimplenteCTN(
        matricula=_texto(linha["matricula"]),
        nome=_texto(linha["nome"]),
        telefone=normalizar_telefone(str(linha.get("telefone") or "")) or "",
        meses_atraso=_inteiro(linha["meses_atraso"]),
        meses_ate_corte=_inteiro(linha["meses_ate_corte"]),
        ult_mesref=_data_ou_none(linha.get("ult_mesref")),
        data_ref=_data(linha["data_ref"]),
    )


def _matricula_legivel(linha: Mapping) -> str | None:
    try:
        return _texto(linha["matricula"])
    except (KeyError, TypeError, ValueError):
        return None


def _baixa(linha: Mapping) -> BaixaCTN:
    return BaixaCTN(
        id_externo=_texto(str(linha["id"])),
        matricula=_texto(linha["matricula"]),
        mes_ref=_mes(linha["mes_ref"]),
        valor_centavos=_centavos(linha["valor"]),
        forma=forma_do_historico(linha.get("forma")),
        pago_em=_instante(linha["dt_baixa"]),
        coletado_em=_instante(linha["coletado_em"]),
    )


def _lancamento(linha: Mapping) -> tuple[MensalidadeAberta, date | None]:
    return (
        MensalidadeAberta(_mes(linha["mes_ano"]), _centavos(linha["valor"])),
        _data_ou_none(linha.get("data_baixa")),
    )


def _como_utc(instante: datetime) -> datetime:
    return instante if instante.tzinfo is not None else instante.replace(tzinfo=UTC)


def _decrescente_provado(baixas: list[BaixaCTN]) -> bool:
    """A página está em `coletado_em` decrescente: nenhum par sobe e ao menos um desce. Uma página
    toda do mesmo instante (ou de uma baixa só) não prova nada: a API pode ter devolvido a ordem
    crescente, e a página, a dos mais antigos."""
    pares = list(zip(baixas, baixas[1:], strict=False))
    return all(a.coletado_em >= b.coletado_em for a, b in pares) and any(
        a.coletado_em > b.coletado_em for a, b in pares
    )


class CTNEficaz:
    """Só leitura. `transporte` serve ao `httpx.MockTransport` dos testes; `relogio` e `dormir`
    são do limite de taxa, e `agora`, do "hoje" do histórico e da janela das baixas."""

    def __init__(
        self,
        url: str,
        chave: str,
        unidade: str,
        transporte: httpx.BaseTransport | None = None,
        limite_minuto: int = LIMITE_POR_MINUTO,
        *,
        linhas_por_pagina: int = LINHAS_POR_PAGINA,
        prazo: float = PRAZO_PADRAO,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
        agora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if limite_minuto < 1:
            raise ValueError("o limite de chamadas por minuto do CTN tem de ser positivo")
        if not 1 <= linhas_por_pagina <= LINHAS_MAXIMAS:
            raise ValueError(f"as linhas por página do CTN vão de 1 a {LINHAS_MAXIMAS}")
        self.unidade = unidade
        # O total de chamadas usadas hoje, como a API informa no campo `uso` (None sem resposta).
        self.ultimo_uso: Mapping | None = None
        self._limite_minuto = limite_minuto
        self._linhas_por_pagina = linhas_por_pagina
        self._prazo = prazo
        self._relogio = relogio
        self._dormir = dormir
        self._agora = agora
        # O laço das rotinas e os turnos chamam de threads diferentes: o limite é um só.
        self._trava_taxa = threading.Lock()
        self._chamadas: deque[float] = deque()
        # matrícula -> (instante do `relogio` em que expira, as baixas da pessoa)
        self._trava_cache = threading.Lock()
        self._baixas_em_cache: dict[str, tuple[float, tuple[BaixaCTN, ...]]] = {}
        self._cliente = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {chave}", "Accept": "application/json"},
            timeout=prazo,
            transport=transporte,
        )

    @classmethod
    def da_config(
        cls, config: ConfigCTN, transporte: httpx.BaseTransport | None = None, **extra: Any
    ) -> "CTNEficaz":
        if not config.ctn_api_key:
            raise ValueError("o CTN real precisa da AGENTE_CTN_API_KEY")
        return cls(
            config.ctn_url,
            config.ctn_api_key,
            config.ctn_unidade,
            transporte=transporte,
            **extra,
        )

    def __enter__(self) -> "CTNEficaz":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

    # ---- leitura: o Protocol CTN ----

    def consultar_cliente(self, matricula: str) -> ClienteCTN:
        linhas = self._linhas(ROTA_FILIADOS, {"matricula": matricula}, "matricula", matricula)
        linha = next((x for x in linhas if x.get("matricula") == matricula), None)
        if linha is None:
            raise ClienteNaoEncontrado(matricula)
        return _converter(
            linha,
            lambda x: ClienteCTN(
                matricula=matricula,
                nome=_texto(x["nome"]),
                telefone=normalizar_telefone(str(x.get("telefone") or "")) or "",
                cpf=_cpf(x.get("cpf")),
            ),
        )

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        """Matrícula desconhecida: lista vazia (a API não distingue "sem baixas" de "sem a
        matrícula"); só o `consultar_cliente` levanta ClienteNaoEncontrado."""
        mes_atual = self._hoje().year * 12 + self._hoje().month
        pagamentos = []
        for baixa in self._baixas_da_pessoa(matricula):
            if mes_atual - (baixa.pago_em.year * 12 + baixa.pago_em.month) < meses:
                pagamentos.append(
                    PagamentoHistorico(baixa.pago_em.date(), baixa.forma, baixa.valor_centavos)
                )
        return sorted(pagamentos, key=lambda pagamento: pagamento.data)

    def inadimplentes(
        self, data_ref: date, invalidas: list[str | None] | None = None
    ) -> Iterator[InadimplenteCTN]:
        """Uma linha malformada (sem nome, com `meses_ate_corte` nulo, data ruim...) não interrompe
        o retrato: é pulada, e a matrícula dela, se legível (senão `None`), vai para `invalidas`.
        A falha geral de formato (corpo sem `dados`, `mais` que não é booleano) segue levantando."""
        dia = data_ref.isoformat()
        for linha in self._linhas(ROTA_INADIMPLENCIA, {"de": dia, "ate": dia}, "matricula"):
            try:
                inadimplente = _inadimplente(linha)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                if invalidas is not None:
                    invalidas.append(_matricula_legivel(linha))
                continue
            yield inadimplente

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        return self.consultar_mensalidades(matricula).abertas

    def consultar_mensalidades(self, matricula: str) -> ConsultaMensalidades:
        """Matrícula desconhecida: `ConsultaMensalidades([], 0)` (a API não distingue "sem
        lançamentos" de "sem a matrícula"). As `rejeitada` menos as pagas depois da recusa: o
        `lancamentos` não muda no mesmo dia (spec, seção 2), e a baixa nova está no
        `lancamentos_bx`. Cada baixa paga um lançamento só: a do mesmo mês, de data igual ou
        posterior à do lançamento e de valor igual ou maior (a parcial não tira o mês). Os
        lançamentos mais antigos casam primeiro. `rejeitadas_lidas` separa "nenhuma rejeitada" (0)
        de "todas pagas" (maior que 0, com `abertas` vazia).

        Só valem as linhas da matrícula pedida (`matricula_titular` nos lançamentos e `matricula`
        nas baixas): a resposta de outra pessoa nunca paga nem abre um mês desta."""
        filtros = {"matricula_titular": matricula, "status_lancamento": STATUS_REJEITADA}
        rejeitadas = [
            _converter(linha, _lancamento)
            for linha in self._linhas(ROTA_LANCAMENTOS, filtros, "id")
            if linha.get("status_lancamento") == STATUS_REJEITADA
            and linha.get("matricula_titular") == matricula
        ]
        if not rejeitadas:
            return ConsultaMensalidades([], 0)
        baixas = self._baixas_da_pessoa(matricula)

        # Na API real todo lançamento rejeitado traz a `data_baixa`; sem ela (só nos testes e em
        # dado malformado), qualquer baixa do mês vale, e o lançamento casa primeiro.
        pendentes = sorted(rejeitadas, key=lambda r: (r[1] or date.min, r[0].mes_ano))

        def serve(
            baixa: BaixaCTN, mensalidade: MensalidadeAberta, recusada_em: date | None
        ) -> bool:
            return (
                baixa.mes_ref == mensalidade.mes_ano
                and baixa.valor_centavos >= mensalidade.valor_centavos
                and (recusada_em is None or baixa.pago_em.date() >= recusada_em)
            )

        # Casamento máximo entre lançamentos e baixas (caminhos de aumento): um pareamento guloso
        # deixaria em aberto quem já pagou. Um lançamento casado nunca perde a baixa; só troca de
        # baixa, então os mais antigos, que entram primeiro, são os que ficam pagos se faltar baixa.
        dono_da_baixa: dict[int, int] = {}

        def casar(indice: int, vistas: set[int]) -> bool:
            mensalidade, recusada_em = pendentes[indice]
            for b, baixa in enumerate(baixas):
                if b in vistas or not serve(baixa, mensalidade, recusada_em):
                    continue
                vistas.add(b)
                if b not in dono_da_baixa or casar(dono_da_baixa[b], vistas):
                    dono_da_baixa[b] = indice
                    return True
            return False

        for indice in range(len(pendentes)):
            casar(indice, set())
        pagas = set(dono_da_baixa.values())
        abertas = [m for i, (m, _) in enumerate(pendentes) if i not in pagas]
        return ConsultaMensalidades(
            sorted(abertas, key=lambda mensalidade: mensalidade.mes_ano), len(rejeitadas)
        )

    def baixas_desde(self, coletado_desde: datetime) -> list[BaixaCTN]:
        """As baixas com `coletado_em` a partir do cursor, inclusive (uma baixa coletada no mesmo
        instante do cursor não se perde; o consumidor é idempotente pelo `id_externo`).

        A API filtra por `dia_relatorio`, e o relatório de um dia pode ser coletado depois da
        meia-noite: a janela abre um dia antes do dia do cursor (em Brasília) e anda de 31 em 31
        dias até hoje. Dentro de cada janela a leitura é incremental (`_baixas_da_janela`): no caso
        comum, uma chamada, em vez de reler os dias inteiros a cada 10 minutos (a cota é de 20.000
        linhas por dia)."""
        cursor = _como_utc(coletado_desde)
        hoje = self._hoje()
        inicio = cursor.astimezone(FUSO_DO_NEGOCIO).date() - timedelta(days=1)
        achadas: dict[str, BaixaCTN] = {}
        while inicio <= hoje:
            fim = min(inicio + timedelta(days=DIAS_POR_JANELA - 1), hoje)
            filtros = {"de": inicio.isoformat(), "ate": fim.isoformat()}
            for baixa in self._baixas_da_janela(filtros, cursor):
                achadas[baixa.id_externo] = baixa
            inicio = fim + timedelta(days=1)
        return sorted(achadas.values(), key=lambda b: (b.coletado_em, b.id_externo))

    def _baixas_da_janela(self, filtros: Mapping[str, str], cursor: datetime) -> list[BaixaCTN]:
        """As baixas da janela coletadas a partir do cursor.

        Caminho curto: pede a 1ª página por `coletado_em` decrescente, e as mais novas vêm
        primeiro. Ela basta quando (a) é a janela inteira (`mais = false`), ou (b) traz uma baixa
        anterior ao cursor e a ordem decrescente está PROVADA na própria página: tudo o que não veio
        é mais antigo ainda. Só uma página de 50 aguenta a ordem por `coletado_em`: a paginação por
        uma chave com repetição (uma coleta grava muitas linhas no mesmo instante) pode pular ou
        repetir linhas de uma página a outra, e uma baixa pulada é uma cobrança que não para.

        Caminho longo: a página toda é nova (50 ou mais baixas desde o cursor), a ordem não está
        provada (a API ignorou o `desc`, ou a página inteira é do mesmo instante) ou a API recusou
        o `desc`. Relê a janela inteira pelo `id`, que é único e pagina sem falha, e filtra pelo
        cursor."""
        try:
            pagina, mais = next(self._paginas(ROTA_BAIXAS, filtros, "coletado_em", "desc"))
            baixas = [_converter(linha, _baixa) for linha in pagina]
        except CTNRecusou as erro:
            if erro.status != 400:
                raise
            log.warning("o CTN recusou a ordem decrescente das baixas; lendo a janela inteira")
            return self._janela_inteira_das_baixas(filtros, cursor)
        if not mais:
            return [baixa for baixa in baixas if baixa.coletado_em >= cursor]
        if baixas and baixas[-1].coletado_em < cursor and _decrescente_provado(baixas):
            return [baixa for baixa in baixas if baixa.coletado_em >= cursor]
        return self._janela_inteira_das_baixas(filtros, cursor)

    def _janela_inteira_das_baixas(
        self, filtros: Mapping[str, str], cursor: datetime
    ) -> list[BaixaCTN]:
        baixas = (_converter(linha, _baixa) for linha in self._linhas(ROTA_BAIXAS, filtros, "id"))
        return [baixa for baixa in baixas if baixa.coletado_em >= cursor]

    # ---- HTTP ----

    def _baixas_da_pessoa(self, matricula: str) -> tuple[BaixaCTN, ...]:
        """As baixas da matrícula, lidas uma vez e reaproveitadas por `VALIDADE_DO_CACHE_DE_BAIXAS`
        (hora do `relogio`): servem ao casamento dos lançamentos e ao histórico. A consulta não leva
        período: o `de`/`ate` da rota filtra o `dia_relatorio` (não a `dt_baixa`) e vai a 31 dias, e
        uma leitura só da pessoa cobre o casamento e os 12 meses do histórico. Só as linhas da
        matrícula pedida entram. A falha não fica no cache. O cache guarda no máximo
        `CACHE_BAIXAS_MAXIMO` pessoas."""
        agora = self._relogio()
        with self._trava_cache:
            achado = self._baixas_em_cache.get(matricula)
            if achado is not None and agora < achado[0]:
                return achado[1]
        baixas = tuple(
            _converter(linha, _baixa)
            for linha in self._linhas(ROTA_BAIXAS, {"matricula": matricula}, "id")
            if linha.get("matricula") == matricula
        )
        with self._trava_cache:
            self._baixas_em_cache = {
                chave: valor for chave, valor in self._baixas_em_cache.items() if agora < valor[0]
            }
            while len(self._baixas_em_cache) >= CACHE_BAIXAS_MAXIMO:
                del self._baixas_em_cache[next(iter(self._baixas_em_cache))]
            self._baixas_em_cache[matricula] = (agora + VALIDADE_DO_CACHE_DE_BAIXAS, baixas)
        return baixas

    def _hoje(self) -> date:
        return _como_utc(self._agora()).astimezone(FUSO_DO_NEGOCIO).date()

    def _linhas(
        self,
        rota: str,
        filtros: Mapping[str, str],
        ordem: str,
        matricula: str | None = None,
        direcao: str = "asc",
    ) -> Iterator[Mapping]:
        """As linhas de uma rota, página a página até `mais = false` (ver `_paginas`)."""
        for dados, _mais in self._paginas(rota, filtros, ordem, direcao, matricula):
            yield from dados

    def _paginas(
        self,
        rota: str,
        filtros: Mapping[str, str],
        ordem: str,
        direcao: str = "asc",
        matricula: str | None = None,
    ) -> Iterator[tuple[list[Mapping], bool]]:
        """As páginas de uma rota, cada uma como `(linhas, mais)`, até `mais = false`. A paginação
        sem `ordem` repete linhas (sondagem de 2026-11-04), então toda consulta manda `ordem`.
        `matricula` é a pessoa da consulta do cadastro: o 404 dela é "não existe"; todo 400 é falha
        do CTN.

        A paginação falha fechada: `mais` tem de ser booleano, uma página vazia com `mais` não pode
        seguir (laço) e passar de `PAGINAS_MAXIMAS_POR_ROTA` páginas é falha. Quem só quer a 1ª
        página pede a próxima e fecha o gerador: as demais não são chamadas."""
        for pagina in range(PAGINAS_MAXIMAS_POR_ROTA):
            params = {
                **filtros,
                "unidade": self.unidade,
                "ordem": ordem,
                "direcao": direcao,
                "pagina": pagina,
                "limite": self._linhas_por_pagina,
            }
            corpo = self._obter(rota, params, matricula)
            dados = corpo.get("dados") if isinstance(corpo, Mapping) else None
            if not isinstance(dados, list) or not all(isinstance(x, Mapping) for x in dados):
                raise _invalida()
            mais = corpo.get("mais")
            if not isinstance(mais, bool) or (mais and not dados):
                raise _invalida()
            uso = corpo.get("uso")
            self.ultimo_uso = uso if isinstance(uso, Mapping) else None
            log.debug("CTN %s página %s: %s linhas, uso %s", rota, pagina, len(dados), uso)
            yield dados, mais
            if not mais:
                return
        raise CTNIndisponivel("o CTN passou do limite de páginas")

    def _esperar_vaga(self) -> None:
        """Até `limite_minuto` chamadas em qualquer janela de 60 s, seguro entre threads: quem
        passa do limite dorme, com a trava, até a chamada mais antiga sair da janela. A espera
        tem teto próprio (`JANELA_DO_LIMITE` + o prazo, na hora do `relogio`) e não conta no prazo
        da requisição: um laço de mais de `limite_minuto` chamadas dorme e termina. Se a espera
        passar do teto (outras threads encheram a janela), levanta CTNIndisponivel, sem dormir."""
        teto = self._relogio() + JANELA_DO_LIMITE + self._prazo
        if not self._trava_taxa.acquire(timeout=max(0.0, teto - self._relogio())):
            raise CTNIndisponivel(MOTIVO_PRAZO)
        try:
            while True:
                agora = self._relogio()
                while self._chamadas and agora - self._chamadas[0] >= JANELA_DO_LIMITE:
                    self._chamadas.popleft()
                if len(self._chamadas) < self._limite_minuto:
                    self._chamadas.append(agora)
                    return
                espera = JANELA_DO_LIMITE - (agora - self._chamadas[0])
                if agora + espera > teto:
                    raise CTNIndisponivel(MOTIVO_PRAZO)
                self._dormir(espera)
        finally:
            self._trava_taxa.release()

    def _obter(self, rota: str, params: Mapping[str, Any], matricula: str | None) -> object:
        """Um GET: espera a vaga do limite de taxa (teto próprio) e então vale o prazo de
        `self._prazo` para a requisição (conexão e corpo juntos).

        O erro nunca carrega o erro de origem: o `CTNIndisponivel` sai de fora dos `except`, com
        `__context__` e `__cause__` nulos, porque a mensagem do httpx pode ecoar a URL.
        """
        self._esperar_vaga()
        limite = self._relogio() + self._prazo
        try:
            with self._cliente.stream("GET", rota, params=params) as resposta:
                # O status vem antes do corpo: o erro nunca lê o que o servidor ecoou.
                if matricula is not None and resposta.status_code == 404:
                    raise ClienteNaoEncontrado(matricula)
                if not resposta.is_success:
                    raise CTNRecusou(resposta.status_code)
                pedacos = []
                for pedaco in resposta.iter_bytes():
                    pedacos.append(pedaco)
                    if self._relogio() > limite:
                        raise CTNIndisponivel(MOTIVO_PRAZO)
            if self._relogio() > limite:
                motivo = MOTIVO_PRAZO
            else:
                return json.loads(b"".join(pedacos))
        except (ClienteNaoEncontrado, CTNIndisponivel):
            raise
        except httpx.TimeoutException:
            motivo = MOTIVO_PRAZO
        except httpx.HTTPError as erro:
            motivo = f"falha de rede com o CTN ({type(erro).__name__})"
        except ValueError:
            motivo = "a resposta do CTN não é JSON"
        raise CTNIndisponivel(motivo) from None
