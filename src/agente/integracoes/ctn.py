"""Interface do CTN, o sistema com os clientes, as matrículas e o histórico (D31).

A implementação real depende da T9 (tarefa 6.5). Até lá, o sistema roda com o CTNFalso.
"""

import secrets
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from agente.tipos import FormaHistorico, PagamentoHistorico


@dataclass(frozen=True)
class ClienteCTN:
    matricula: str
    nome: str
    telefone: str
    cpf: str | None


@dataclass(frozen=True)
class LinkPagamento:
    url: str
    id_externo: str
    expira_em: datetime | None = None


@dataclass(frozen=True)
class InadimplenteCTN:
    """Uma linha do retrato diário da inadimplência. `meses_atraso` + `meses_ate_corte` = 6.

    `telefone` vem em E.164, ou vazio se o CTN deu um número que não é brasileiro.
    """

    matricula: str
    nome: str
    telefone: str
    meses_atraso: int
    meses_ate_corte: int
    ult_mesref: date | None
    data_ref: date


@dataclass(frozen=True)
class MensalidadeAberta:
    """Uma mensalidade que o CTN recusou e que ainda não foi paga. `mes_ano` é `AAAA/MM`."""

    mes_ano: str
    valor_centavos: int


@dataclass(frozen=True)
class BaixaCTN:
    """Uma baixa que o CTN registrou, de qualquer caminho (não só as nossas). `mes_ref` é `AAAA/MM`;
    `coletado_em` é o cursor da rotina das baixas."""

    id_externo: str
    matricula: str
    mes_ref: str
    valor_centavos: int
    forma: FormaHistorico
    pago_em: datetime
    coletado_em: datetime


class ClienteNaoEncontrado(LookupError):
    """A matrícula não existe no CTN."""


class CTNIndisponivel(Exception):
    """O CTN não respondeu (fora do ar, prazo estourado ou erro 5xx). Toda implementação do `CTN`
    levanta esta exceção nessa falha; o turno a trata como integração fora do ar."""


class CTN(Protocol):
    def consultar_cliente(self, matricula: str) -> ClienteCTN: ...

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        """Os pagamentos dos últimos `meses` meses, do mais antigo ao mais novo. Matrícula
        desconhecida devolve a lista vazia (o CTN real não distingue "sem baixas" de "sem a
        matrícula"); só o `consultar_cliente` levanta ClienteNaoEncontrado."""
        ...

    def inadimplentes(self, data_ref: date) -> Iterator[InadimplenteCTN]:
        """O retrato da inadimplência de um dia, página a página. Se o CTN cair no meio, o
        iterador levanta CTNIndisponivel: quem consome sabe que o retrato veio pela metade."""
        ...

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        """As mensalidades recusadas e ainda sem baixa, do mês mais antigo ao mais novo. Matrícula
        desconhecida devolve a lista vazia, como no `historico_pagamentos`."""
        ...

    def baixas_desde(self, coletado_desde: datetime) -> list[BaixaCTN]:
        """As baixas coletadas a partir de `coletado_desde`, INCLUSIVE, da coletada há mais tempo
        à mais nova, sem `id_externo` repetido. O cursor é inclusivo porque duas baixas podem ter a
        mesma `coletado_em`: com `>` a que chegasse depois do cursor andar se perderia. Quem guarda
        o cursor recebe de volta a última baixa já vista e a descarta pelo `id_externo` (a rotina da
        Etapa 3 é idempotente por ele)."""
        ...

    def gerar_link_pagamento(
        self, matricula: str, valor_centavos: int, cobranca_id: int
    ) -> LinkPagamento:
        """O link em que o cliente paga e já cadastra o cartão (D30)."""
        ...

    def gerar_link_cadastro_cartao(self, matricula: str) -> LinkPagamento:
        """O link só para cadastrar o cartão, sem pagar (G9.3)."""
        ...

    def registrar_baixa(
        self, matricula: str, valor_centavos: int, pago_em: datetime, referencia: str
    ) -> bool:
        """Dá a baixa na aba de mensalidades. Devolve False se a referência já tinha baixa."""
        ...


def _exigir_valor(valor_centavos: int) -> None:
    if valor_centavos <= 0:
        raise ValueError(f"o valor do link tem de ser positivo, veio {valor_centavos}")


class CTNFalso:
    """Responde como o CTN responderia e grava cada chamada para os testes."""

    def __init__(
        self,
        clientes: Iterable[ClienteCTN] = (),
        historicos: dict[str, list[PagamentoHistorico]] | None = None,
        hoje: date | None = None,
        aceita_qualquer_matricula: bool = False,
        retrato: Iterable[InadimplenteCTN] = (),
        mensalidades_abertas: dict[str, list[MensalidadeAberta]] | None = None,
        baixas_externas: Iterable[BaixaCTN] = (),
    ):
        # Até a T9 o serviço no ar usa o CTN falso sem a carteira carregada nele: com isto ligado,
        # a matrícula desconhecida vira um cliente sem CPF, e o link falso (ctn.invalid) sai.
        self.aceita_qualquer_matricula = aceita_qualquer_matricula
        self.clientes = {cliente.matricula: cliente for cliente in clientes}
        self.historicos = historicos or {}
        self.hoje = hoje or date.today()
        self.retrato = list(retrato)
        self.mensalidades_abertas = mensalidades_abertas or {}
        self.baixas_externas = list(baixas_externas)
        self.links: dict[str, tuple[str, int | None, int | None]] = {}
        # O contador volta a 1 a cada processo, e o banco da demo guarda links de rodadas antigas:
        # sem um lote único, o 1º link depois de reiniciar repetia o `id_externo` e quebrava o
        # turno (A11, `pagamento.referencia` é única). O CTN real devolve o próprio id.
        self._lote = secrets.token_hex(3)
        self.baixas: list[tuple[str, int, datetime, str]] = []
        # Liga a falha do CTN: toda chamada levanta CTNIndisponivel.
        self.fora_do_ar = False

    def _exigir_no_ar(self) -> None:
        if self.fora_do_ar:
            raise CTNIndisponivel("CTN fora do ar (simulado)")

    def consultar_cliente(self, matricula: str) -> ClienteCTN:
        self._exigir_no_ar()
        try:
            return self.clientes[matricula]
        except KeyError:
            if self.aceita_qualquer_matricula:
                return ClienteCTN(matricula, "Cliente", "+550000000000", None)
            raise ClienteNaoEncontrado(matricula) from None

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        self._exigir_no_ar()
        mes_atual = self.hoje.year * 12 + self.hoje.month
        return sorted(
            (
                pagamento
                for pagamento in self.historicos.get(matricula, [])
                if mes_atual - (pagamento.data.year * 12 + pagamento.data.month) < meses
            ),
            key=lambda pagamento: pagamento.data,
        )

    def inadimplentes(self, data_ref: date) -> Iterator[InadimplenteCTN]:
        self._exigir_no_ar()
        return iter([linha for linha in self.retrato if linha.data_ref == data_ref])

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        self._exigir_no_ar()
        return sorted(self.mensalidades_abertas.get(matricula, []), key=lambda m: m.mes_ano)

    def baixas_desde(self, coletado_desde: datetime) -> list[BaixaCTN]:
        self._exigir_no_ar()
        unicas = {
            baixa.id_externo: baixa
            for baixa in self.baixas_externas
            if baixa.coletado_em >= coletado_desde
        }
        return sorted(unicas.values(), key=lambda baixa: (baixa.coletado_em, baixa.id_externo))

    def _novo_link(self, tipo: str, matricula: str, valor: int | None, cobranca: int | None):
        id_externo = f"{tipo}-{self._lote}-{len(self.links) + 1}"
        self.links[id_externo] = (matricula, valor, cobranca)
        return LinkPagamento(url=f"https://ctn.invalid/{tipo}/{id_externo}", id_externo=id_externo)

    def gerar_link_pagamento(
        self, matricula: str, valor_centavos: int, cobranca_id: int
    ) -> LinkPagamento:
        self.consultar_cliente(matricula)
        _exigir_valor(valor_centavos)
        return self._novo_link("pagar", matricula, valor_centavos, cobranca_id)

    def gerar_link_cadastro_cartao(self, matricula: str) -> LinkPagamento:
        self.consultar_cliente(matricula)
        return self._novo_link("cartao", matricula, None, None)

    def registrar_baixa(
        self, matricula: str, valor_centavos: int, pago_em: datetime, referencia: str
    ) -> bool:
        self.consultar_cliente(matricula)
        if any(baixa[3] == referencia for baixa in self.baixas):
            return False
        self.baixas.append((matricula, valor_centavos, pago_em, referencia))
        return True


class CTNComposto:
    """O CTN real só de leitura (EFICAZ) com a escrita ainda falsa.

    As 5 leituras vão a `leitura` (o `CTNEficaz` ou um falso); o link de pagamento, o link de
    cadastro do cartão e a baixa vão a `escrita` (o `CTNFalso`), porque a API não tem escrita.
    """

    def __init__(self, leitura: CTN, escrita: CTN):
        self.leitura = leitura
        self.escrita = escrita

    def consultar_cliente(self, matricula: str) -> ClienteCTN:
        return self.leitura.consultar_cliente(matricula)

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        return self.leitura.historico_pagamentos(matricula, meses)

    def inadimplentes(self, data_ref: date) -> Iterator[InadimplenteCTN]:
        return self.leitura.inadimplentes(data_ref)

    def mensalidades_em_aberto(self, matricula: str) -> list[MensalidadeAberta]:
        return self.leitura.mensalidades_em_aberto(matricula)

    def baixas_desde(self, coletado_desde: datetime) -> list[BaixaCTN]:
        return self.leitura.baixas_desde(coletado_desde)

    def gerar_link_pagamento(
        self, matricula: str, valor_centavos: int, cobranca_id: int
    ) -> LinkPagamento:
        return self.escrita.gerar_link_pagamento(matricula, valor_centavos, cobranca_id)

    def gerar_link_cadastro_cartao(self, matricula: str) -> LinkPagamento:
        return self.escrita.gerar_link_cadastro_cartao(matricula)

    def registrar_baixa(
        self, matricula: str, valor_centavos: int, pago_em: datetime, referencia: str
    ) -> bool:
        return self.escrita.registrar_baixa(matricula, valor_centavos, pago_em, referencia)

    def fechar(self) -> None:
        """Fecha o cliente HTTP da leitura, se ela tiver um; o falso não tem o que fechar."""
        fechar = getattr(self.leitura, "fechar", None)
        if callable(fechar):
            fechar()
