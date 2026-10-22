"""A avaliação do turno: Jev 1, gatilhos de saída, Jev 2 e as regras de ação (tarefas 4.2 e 4.3).

A conversa EM_RISCO tem um turno para recuperar: sem melhora, vai para o humano (seção 3).

Não grava nem envia nada: o orquestrador grava o resultado, e a simulação compara com o esperado.
Com o Jev indisponível, o turno vai para o humano, nunca para o robô sem avaliação (seção 9).
"""

from dataclasses import dataclass

from agente.db.modelos import Cobranca, Conversa
from agente.dominio.desconto import Oferta, proxima_oferta, valor_com_desconto
from agente.integracoes.jev import EstadoJev, Jev, JevIndisponivel
from agente.politica import Politica
from agente.tipos import BatchJev, EstadoConversa, Laudo, MotivoHumano
from agente.turno.acoes import Acao, Decisao, autorizar
from agente.turno.jev1 import (
    PERGUNTA_DO_GATILHO,
    LeituraJev1,
    em_risco,
    gatilho_de_saida,
    laudo_do_risco,
    ler,
)
from agente.turno.jev2 import decidir

# O prefixo da ação principal do turno cujo texto não saiu: nada do que ela fez ficou valendo.
NAO_EXECUTADA = "NAO_EXECUTADA:"


@dataclass(frozen=True)
class Avaliacao:
    """A ação autorizada e os anexos, ou o motivo de ir para o humano."""

    leitura: LeituraJev1 | None
    decisao: Decisao | None
    acao: Acao | None
    anexos: list[Acao]
    motivo: MotivoHumano | None
    erro: str | None = None

    @property
    def rotulo(self) -> str:
        """O que o turno grava como ação principal: a ação ou `HUMAN:<motivo>`."""
        return self.acao.value if self.acao is not None else f"HUMAN:{self.motivo.value}"

    @property
    def rotulo_nao_executado(self) -> str:
        """A ação decidida cujo texto não saiu: o backend desfez o que ela tinha feito."""
        return f"{NAO_EXECUTADA}{self.rotulo}"

    @property
    def causa(self) -> tuple[BatchJev, Laudo] | None:
        """O laudo que levou ao humano, com o batch, para a nota e a transição (auditoria)."""
        if self.motivo is None or self.leitura is None:
            return None
        if self.motivo is MotivoHumano.OBJECAO_SEM_DESCONTO:
            return BatchJev.JEV1, self.leitura.laudo("objecao_valor")
        if self.motivo is MotivoHumano.SEM_MELHORA:
            return BatchJev.JEV1, laudo_do_risco(self.leitura)
        if self.decisao is not None:
            return BatchJev.JEV2, self.decisao.laudo
        # Sem decisão, só o gatilho do Jev 1 tem laudo; o Jev 2 indisponível, não.
        pergunta = PERGUNTA_DO_GATILHO.get(self.motivo)
        return (BatchJev.JEV1, self.leitura.laudo(pergunta)) if pergunta is not None else None

    @property
    def em_risco(self) -> bool:
        return self.leitura is not None and em_risco(self.leitura)


def oferta_no_degrau(cobranca: Cobranca, degrau: int, politica: Politica) -> Oferta | None:
    if degrau == 0:
        return None
    valor = valor_com_desconto(cobranca.regua, cobranca.divida_centavos, degrau, politica)
    return Oferta(degrau, cobranca.divida_centavos - valor, valor)


def estado_jev(
    conversa: Conversa,
    cobranca: Cobranca,
    historico: list[tuple[str, str]],
    politica: Politica,
) -> EstadoJev:
    return EstadoJev(
        historico=tuple(historico),
        regua=cobranca.regua,
        divida_centavos=cobranca.divida_centavos,
        estado_conversa=conversa.estado,
        oferta_atual=oferta_no_degrau(cobranca, conversa.degrau_atual, politica),
    )


def avaliar(
    jev: Jev, estado: EstadoJev, conversa: Conversa, cobranca: Cobranca, politica: Politica
) -> Avaliacao:
    leitura = None
    try:
        leitura = ler(jev, estado)
        gatilho = gatilho_de_saida(leitura, politica)
        if gatilho is not None:
            # Na saída, o robô se cala no mesmo turno: o Jev 2 nem roda.
            return Avaliacao(leitura, None, None, [], gatilho)
        if conversa.estado is EstadoConversa.EM_RISCO and em_risco(leitura):
            # O turno de recuperação não melhorou: sai para o humano, sem decidir nem redigir.
            return Avaliacao(leitura, None, None, [], MotivoHumano.SEM_MELHORA)
        decisao = decidir(jev, estado, leitura)
    except JevIndisponivel as erro:
        return Avaliacao(leitura, None, None, [], MotivoHumano.JEV_INDISPONIVEL, str(erro))
    acao, anexos = autorizar(decisao, conversa, cobranca, leitura, politica)
    if isinstance(acao, MotivoHumano):
        return Avaliacao(leitura, decisao, None, [], acao)
    return Avaliacao(leitura, decisao, acao, anexos, None)


def numeros_previstos(
    avaliacao: Avaliacao, conversa: Conversa, cobranca: Cobranca, politica: Politica
) -> dict:
    """A prévia da sombra: os números que a ação teria levado, sem gerar link nem Pix.

    No modo ativo, quem gera os números de verdade é o `executar`.
    """
    if avaliacao.acao is None:
        return {"erro": avaliacao.erro} if avaliacao.erro else {}
    acoes = [avaliacao.acao, *avaliacao.anexos]
    numeros: dict = {"divida_centavos": cobranca.divida_centavos}
    degrau = conversa.degrau_atual
    if Acao.OFFER_DISCOUNT in acoes:
        oferta = proxima_oferta(cobranca.regua, cobranca.divida_centavos, degrau, politica)
        numeros |= {
            "degrau": oferta.degrau,
            "desconto_centavos": oferta.desconto_centavos,
            "valor_final_centavos": oferta.valor_final_centavos,
        }
        degrau = oferta.degrau
    if Acao.SEND_CTN_LINK in acoes:
        numeros["valor_link_centavos"] = valor_com_desconto(
            cobranca.regua, cobranca.divida_centavos, degrau, politica
        )
    if Acao.GENERATE_PIX in acoes:
        # O Pix paga sempre o integral (D34).
        numeros["valor_pix_centavos"] = cobranca.divida_centavos
    return numeros
