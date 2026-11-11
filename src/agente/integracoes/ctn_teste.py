"""O CTN do modo teste (D56): os casos do cardápio com o formato do CTN, e o roteador.

Cada testador vira uma matrícula simulada (`SIM-<caso>-<4 últimos dígitos do telefone>`). O
`CTNDeCasos` responde por elas como o CTN responderia por uma pessoa de verdade, e o `RoteadorCTN`
deixa o resto (a carteira real, o retrato e as baixas) com o CTN real. O link e a baixa do modo
teste são falsos (`CTNEscritaDeTeste`): nada é cobrado de ninguém.
"""

import re
import threading
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn import (
    CTN,
    BaixaCTN,
    ClienteCTN,
    ClienteNaoEncontrado,
    ConsultaMensalidades,
    CTNFalso,
    InadimplenteCTN,
    LinkPagamento,
    MensalidadeAberta,
)
from agente.politica import CasoTeste
from agente.tipos import FormaHistorico, PagamentoHistorico

PREFIXO_SIMULADO = "SIM-"
_MATRICULA = re.compile(rf"^{re.escape(PREFIXO_SIMULADO)}(\d+)-\d{{4}}$")


def matricula_simulada(numero: int, telefone: str) -> str:
    """`SIM-<caso>-<4 últimos dígitos do telefone>`: o testador e o caso numa matrícula só."""
    digitos = re.sub(r"\D", "", telefone)
    if len(digitos) < 4:
        raise ValueError("o telefone do testador tem de ter ao menos 4 dígitos")
    return f"{PREFIXO_SIMULADO}{numero}-{digitos[-4:]}"


def numero_do_caso(matricula: str) -> int | None:
    """O número do caso de uma matrícula `SIM-<n>-<dddd>`; None se ela não tem esse formato."""
    achado = _MATRICULA.match(matricula)
    return int(achado.group(1)) if achado else None


class CasoInexistente(LookupError):
    """A matrícula (ou o número) não é de um caso do cardápio."""


def _indice(dia: date) -> int:
    return dia.year * 12 + dia.month - 1


def _mes(indice: int) -> str:
    return f"{indice // 12:04d}/{indice % 12 + 1:02d}"


@dataclass
class _Estado:
    """O que mudou na matrícula desde que o caso começou: o telefone e os meses já pagos."""

    telefone: str = ""
    pagos: set[str] = field(default_factory=set)
    # As baixas de cada `/paguei <n>`, pela chave dele: o mesmo comando não paga duas vezes.
    baixas: dict[str, list[BaixaCTN]] = field(default_factory=dict)
    # O mês de "hoje" (ano * 12 + mês - 1) em que o caso começou: os meses do caso são relativos a
    # ele, e não ao dia corrente, para o caso não deslizar na virada do mês, no meio da conversa.
    mes_base: int | None = None

    def copia(self) -> "_Estado":
        return _Estado(self.telefone, set(self.pagos), dict(self.baixas), self.mes_base)


class CTNDeCasos:
    """Responde pelas matrículas simuladas do cardápio, relativo ao mês de `hoje()` no `ativar`.

    Só tem as leituras do `CTN`: o retrato e as baixas ficam com o CTN real (vêm vazios aqui), e o
    link e a baixa são do `CTNEscritaDeTeste`. O estado de cada matrícula nasce do cardápio na 1ª
    leitura e vive na memória (P10): reiniciar o serviço volta cada caso ao começo.
    """

    def __init__(
        self,
        casos: Sequence[CasoTeste],
        mensalidade_centavos: int,
        hoje: Callable[[], date],
    ):
        self._casos = {caso.numero: caso for caso in casos}
        self._mensalidade = mensalidade_centavos
        self._hoje = hoje
        self._estados: dict[str, _Estado] = {}
        # Os endpoints do FastAPI rodam em threads: o `/paguei` repetido não pode pagar duas vezes.
        self._trava = threading.Lock()

    def _caso(self, matricula: str) -> CasoTeste | None:
        numero = numero_do_caso(matricula)
        return None if numero is None else self._casos.get(numero)

    def _exigir_caso(self, matricula: str) -> CasoTeste:
        caso = self._caso(matricula)
        if caso is None:
            raise CasoInexistente(matricula)
        return caso

    def _estado(self, matricula: str) -> _Estado:
        return self._estados.setdefault(matricula, _Estado())

    def _indice_do_1o_mes_em_aberto(self, matricula: str, caso: CasoTeste) -> int:
        estado = self._estado(matricula)
        if estado.mes_base is None:  # a matrícula nunca foi ativada: vale o mês da 1ª leitura
            estado.mes_base = _indice(self._hoje())
        return estado.mes_base - caso.meses_atraso

    def _meses_do_caso(self, matricula: str, caso: CasoTeste) -> list[str]:
        primeiro = self._indice_do_1o_mes_em_aberto(matricula, caso)
        return [_mes(primeiro + k) for k in range(caso.meses_atraso)]

    def _abertas(self, matricula: str, caso: CasoTeste) -> list[MensalidadeAberta]:
        pagos = self._estado(matricula).pagos
        return [
            MensalidadeAberta(mes, self._mensalidade)
            for mes in self._meses_do_caso(matricula, caso)
            if mes not in pagos
        ]

    # ---- as leituras do CTN ----

    def consultar_cliente(self, matricula: str) -> ClienteCTN:
        caso = self._caso(matricula)
        if caso is None:
            raise ClienteNaoEncontrado(matricula)
        return ClienteCTN(matricula, caso.nome, self._estado(matricula).telefone, caso.cpf)

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        caso = self._caso(matricula)
        return [] if caso is None else self._abertas(matricula, caso)

    def consultar_mensalidades(self, matricula: str) -> ConsultaMensalidades:
        """`rejeitadas_lidas` é sempre a contagem do caso: com tudo pago, a conferência vê "havia
        rejeitadas e todas foram pagas" (quitada), como no CTN real."""
        caso = self._caso(matricula)
        if caso is None:
            return ConsultaMensalidades([], 0)
        return ConsultaMensalidades(self._abertas(matricula, caso), caso.meses_atraso)

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        """Os pagamentos do caso, em datas reais antes do 1º mês em aberto, na janela de `meses`
        contados de hoje, como o CTN real (a conferência pede os meses até o `ult_mesref`, D58)."""
        caso = self._caso(matricula)
        if caso is None:
            return []
        primeiro = self._indice_do_1o_mes_em_aberto(matricula, caso)
        atual = _indice(self._hoje())
        pagamentos = []
        for item in caso.historico:
            indice = primeiro - item.meses_atras
            if atual - indice >= meses:
                continue
            data = date(indice // 12, indice % 12 + 1, item.dia)
            pagamentos.append(PagamentoHistorico(data, item.forma, self._mensalidade))
        return sorted(pagamentos, key=lambda pagamento: pagamento.data)

    def inadimplentes(
        self, data_ref: date, invalidas: list[str | None] | None = None
    ) -> Iterator[InadimplenteCTN]:
        """A carteira é a do CTN real: os casos nascem no 1º contato, não no retrato."""
        return iter(())

    def baixas_desde(
        self, coletado_desde: datetime, vistos: Collection[str] = ()
    ) -> list[BaixaCTN]:
        """As baixas dos casos não passam pela rotina das baixas do CTN: o `/paguei <n>` as aplica
        direto (`pagar_meses`)."""
        return []

    def invalidar_cache(self, matricula: str) -> None:
        """Não há cache."""

    # ---- as operações do teste ----

    def ativar(self, matricula: str, telefone: str) -> "_Estado | None":
        """Volta a matrícula ao começo do caso, com o telefone do testador, e fixa o mês base no de
        hoje. Devolve o estado anterior (`None` se não havia): quem chama o passa ao `restaurar` se
        o banco desfizer a troca (o estado vive na memória e não volta sozinho no rollback)."""
        self._exigir_caso(matricula)
        with self._trava:
            anterior = self._estados.get(matricula)
            self._estados[matricula] = _Estado(telefone=telefone, mes_base=_indice(self._hoje()))
            return None if anterior is None else anterior.copia()

    def restaurar(self, matricula: str, anterior: "_Estado | None") -> None:
        """Desfaz um `ativar`: o estado volta ao que ele devolveu (sem estado, a matrícula nasce de
        novo na 1ª leitura)."""
        with self._trava:
            if anterior is None:
                self._estados.pop(matricula, None)
            else:
                self._estados[matricula] = anterior

    def ult_mesref(self, matricula: str) -> date:
        """O dia 1 do mês anterior ao 1º mês em aberto: o `ult_mesref` que a cobrança guarda."""
        indice = self._indice_do_1o_mes_em_aberto(matricula, self._exigir_caso(matricula)) - 1
        return date(indice // 12, indice % 12 + 1, 1)

    def pagar_meses(self, matricula: str, n: int, pago_em: datetime, chave: str) -> list[BaixaCTN]:
        """Paga, por boleto, os `n` meses em aberto mais antigos. Idempotente pela `chave`: a 2ª
        chamada com a mesma chave devolve as mesmas baixas, sem pagar de novo. `n` fora de
        1..abertos levanta ValueError."""
        caso = self._exigir_caso(matricula)
        with self._trava:
            estado = self._estado(matricula)
            if chave in estado.baixas:
                return list(estado.baixas[chave])
            abertas = self._abertas(matricula, caso)
            if not 1 <= n <= len(abertas):
                raise ValueError(f"n tem de ir de 1 a {len(abertas)} (meses em aberto), veio {n}")
            baixas = [
                BaixaCTN(
                    id_externo=f"SIM-{chave}-{posicao}",
                    matricula=matricula,
                    mes_ref=aberta.mes_ano,
                    valor_centavos=aberta.valor_centavos,
                    forma=FormaHistorico.BOLETO,
                    pago_em=pago_em,
                    coletado_em=pago_em,
                )
                for posicao, aberta in enumerate(abertas[:n], start=1)
            ]
            estado.pagos.update(baixa.mes_ref for baixa in baixas)
            estado.baixas[chave] = baixas
            return list(baixas)


class RoteadorCTN:
    """A matrícula `SIM-` vai ao CTN dos casos; o resto, ao CTN real.

    O retrato e as baixas são da carteira real: vão só a ela. É um CTN só de leitura (o
    `CTNComposto` manda o link e a baixa ao `CTNEscritaDeTeste`).
    """

    def __init__(self, simulados: CTN, real: CTN):
        self.simulados = simulados
        self.real = real

    def _de(self, matricula: str) -> CTN:
        return self.simulados if matricula.startswith(PREFIXO_SIMULADO) else self.real

    def consultar_cliente(self, matricula: str) -> ClienteCTN:
        return self._de(matricula).consultar_cliente(matricula)

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        return self._de(matricula).historico_pagamentos(matricula, meses)

    def inadimplentes(
        self, data_ref: date, invalidas: list[str | None] | None = None
    ) -> Iterator[InadimplenteCTN]:
        return self.real.inadimplentes(data_ref, invalidas)

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        return self._de(matricula).mensalidades_em_aberto(matricula)

    def consultar_mensalidades(self, matricula: str) -> ConsultaMensalidades:
        return self._de(matricula).consultar_mensalidades(matricula)

    def baixas_desde(
        self, coletado_desde: datetime, vistos: Collection[str] = ()
    ) -> list[BaixaCTN]:
        return self.real.baixas_desde(coletado_desde, vistos)

    def invalidar_cache(self, matricula: str) -> None:
        self._de(matricula).invalidar_cache(matricula)

    def fechar(self) -> None:
        """Fecha o cliente HTTP do CTN real, se ele tiver um."""
        fechar = getattr(self.real, "fechar", None)
        if callable(fechar):
            fechar()


class CTNEscritaDeTeste(CTNFalso):
    """O link e a baixa do modo teste: aceita qualquer matrícula e devolve um texto `[TESTE]` no
    lugar do link. O `id_externo` segue o do falso, porque `pagamento.referencia` é única."""

    def __init__(self):
        super().__init__(aceita_qualquer_matricula=True)

    def _novo_link(self, tipo: str, matricula: str, valor: int | None, cobranca: int | None):
        link = super()._novo_link(tipo, matricula, valor, cobranca)
        if valor is None:
            url = "[TESTE] aqui viria o link para cadastrar o cartão"
        else:
            url = f"[TESTE] aqui viria o link de pagamento de {formatar_reais(valor)}"
        return LinkPagamento(url=url, id_externo=link.id_externo, expira_em=link.expira_em)
