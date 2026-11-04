"""Interface do CTN, o sistema com os clientes, as matrículas e o histórico (D31).

A implementação real depende da T9 (tarefa 6.5). Até lá, o sistema roda com o CTNFalso.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from agente.tipos import PagamentoHistorico


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


class ClienteNaoEncontrado(LookupError):
    """A matrícula não existe no CTN."""


class CTNIndisponivel(Exception):
    """O CTN não respondeu (fora do ar, prazo estourado ou erro 5xx). Toda implementação do `CTN`
    levanta esta exceção nessa falha; o turno a trata como integração fora do ar."""


class CTN(Protocol):
    def consultar_cliente(self, matricula: str) -> ClienteCTN: ...

    def historico_pagamentos(self, matricula: str, meses: int) -> list[PagamentoHistorico]:
        """Os pagamentos dos últimos `meses` meses, do mais antigo ao mais novo."""
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
    ):
        # Até a T9 o serviço no ar usa o CTN falso sem a carteira carregada nele: com isto ligado,
        # a matrícula desconhecida vira um cliente sem CPF, e o link falso (ctn.invalid) sai.
        self.aceita_qualquer_matricula = aceita_qualquer_matricula
        self.clientes = {cliente.matricula: cliente for cliente in clientes}
        self.historicos = historicos or {}
        self.hoje = hoje or date.today()
        self.links: dict[str, tuple[str, int | None, int | None]] = {}
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
        self.consultar_cliente(matricula)
        mes_atual = self.hoje.year * 12 + self.hoje.month
        return sorted(
            (
                pagamento
                for pagamento in self.historicos.get(matricula, [])
                if mes_atual - (pagamento.data.year * 12 + pagamento.data.month) < meses
            ),
            key=lambda pagamento: pagamento.data,
        )

    def _novo_link(self, tipo: str, matricula: str, valor: int | None, cobranca: int | None):
        id_externo = f"{tipo}-{len(self.links) + 1}"
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
