"""O backend executa as ações do turno e produz os números (arquitetura, seção 5).

Todo número que vai ao cliente sai daqui: a dívida, o degrau, o link do CTN e o Pix. Só roda no modo
ativo; em sombra, o turno grava a prévia de `numeros_previstos`, sem gerar link nem Pix.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Protocol

from agente.db.modelos import Cobranca, Conversa, Pagamento
from agente.dominio.desconto import proxima_oferta, valor_com_desconto
from agente.integracoes.ctn import CTN
from agente.integracoes.pix import PixBanco, gerar_txid
from agente.politica import Politica
from agente.tipos import EstadoConversa, FormaPagamento, StatusPagamento
from agente.turno.acoes import Acao


class DependenciasExecutar(Protocol):
    politica: Politica
    ctn: CTN
    pix: PixBanco


@dataclass(frozen=True)
class Numeros:
    """Os números do turno. O valor final é o do link, com o degrau; o Pix é sempre integral."""

    acoes: tuple[Acao, ...]
    divida_centavos: int
    mensalidades: int
    degrau: int
    desconto_centavos: int
    valor_final_centavos: int
    link_ctn: str | None = None
    valor_pix_centavos: int | None = None
    br_code: str | None = None
    pix_vence_em: datetime | None = None
    forma: FormaPagamento | None = None

    def como_dict(self) -> dict:
        """Para o `turno.numeros`, em JSON e sem os campos vazios."""
        dados = asdict(self)
        dados["acoes"] = [acao.value for acao in self.acoes]
        if self.pix_vence_em is not None:
            dados["pix_vence_em"] = self.pix_vence_em.isoformat()
        if self.forma is not None:
            dados["forma"] = self.forma.value
        return {nome: valor for nome, valor in dados.items() if valor is not None}


def contar_objecao(conversa: Conversa, cobranca: Cobranca, politica: Politica) -> None:
    """A objeção respondida na régua sem desconto conta: a que chega ao limite vai ao humano."""
    if politica.reguas[cobranca.regua].desconto_max_centavos == 0:
        conversa.objecoes_sem_desconto += 1


def pix_vigente(cobranca: Cobranca, agora: datetime) -> Pagamento | None:
    """O Pix pendente da cobrança que ainda vale, no valor integral de hoje, ou None."""
    for pagamento in reversed(cobranca.pagamentos):
        if (
            pagamento.forma is FormaPagamento.PIX
            and pagamento.status is StatusPagamento.PENDENTE
            and pagamento.br_code is not None
            and pagamento.valor_centavos == cobranca.divida_centavos
            and pagamento.expira_em is not None
            and pagamento.expira_em > agora
        ):
            return pagamento
    return None


def executar(
    acoes: list[Acao],
    cobranca: Cobranca,
    conversa: Conversa,
    deps: DependenciasExecutar,
    agora: datetime,
    *,
    objecao: bool = False,
) -> Numeros:
    """Grava o degrau, a objeção na régua sem desconto, a forma enviada e o Pix pendente.

    Só muda os objetos: quem chama decide o flush e o commit.
    """
    politica = deps.politica
    regua, divida = cobranca.regua, cobranca.divida_centavos
    if objecao:
        contar_objecao(conversa, cobranca, politica)
    if Acao.OFFER_DISCOUNT in acoes:
        oferta = proxima_oferta(regua, divida, conversa.degrau_atual, politica)
        if oferta is None:
            raise ValueError(f"a régua {regua} não tem degrau seguinte ao {conversa.degrau_atual}")
        conversa.degrau_atual = oferta.degrau
    degrau = conversa.degrau_atual
    valor_final = valor_com_desconto(regua, divida, degrau, politica)
    extras: dict = {}
    if Acao.SEND_CTN_LINK in acoes:
        link = deps.ctn.gerar_link_pagamento(
            cobranca.cliente.matricula_ctn, valor_final, cobranca.id
        )
        extras |= {"link_ctn": link.url, "forma": FormaPagamento.LINK_CTN}
    if Acao.GENERATE_PIX in acoes:
        # O Pix paga sempre o integral (D34). O que ainda vale é reaproveitado: o 2º toque no
        # botão não cria outra cobrança no banco.
        vigente = pix_vigente(cobranca, agora)
        if vigente is None:
            pix = deps.pix.criar_cobranca(
                gerar_txid(cobranca.id),
                divida,
                timedelta(hours=politica.pix_validade_horas),
                cobranca.cliente.nome,
            )
            vigente = Pagamento(
                forma=FormaPagamento.PIX,
                valor_centavos=divida,
                referencia=pix.txid,
                status=StatusPagamento.PENDENTE,
                criado_em=agora,
                expira_em=pix.expira_em,
                br_code=pix.br_code,
            )
            cobranca.pagamentos.append(vigente)
        extras |= {
            "valor_pix_centavos": divida,
            "br_code": vigente.br_code,
            "pix_vence_em": vigente.expira_em.astimezone(politica.janela.zona),
            "forma": FormaPagamento.PIX,
        }
    if "forma" in extras:
        conversa.forma_enviada = extras["forma"]
    return Numeros(
        acoes=tuple(acoes),
        divida_centavos=divida,
        mensalidades=cobranca.mensalidades_abertas,
        degrau=degrau,
        desconto_centavos=divida - valor_final,
        valor_final_centavos=valor_final,
        **extras,
    )


def proximo_estado(
    atual: EstadoConversa, forma: FormaPagamento | None, em_risco: bool
) -> EstadoConversa:
    """O estado depois do turno enviado (seção 3).

    A conversa em risco fica EM_RISCO, com um turno para recuperar; com link ou Pix, ela aguarda o
    pagamento, com a forma; a que só começou ou se recuperou fica engajada.
    """
    if em_risco:
        return EstadoConversa.EM_RISCO
    if forma is not None:
        return EstadoConversa.AGUARDANDO_PAGAMENTO
    if atual in (EstadoConversa.INICIADO, EstadoConversa.EM_RISCO):
        return EstadoConversa.ENGAJADO
    return atual
