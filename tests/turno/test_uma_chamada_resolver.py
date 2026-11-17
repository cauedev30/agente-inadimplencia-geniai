"""O laço das tentativas do turno de uma chamada, com o LLM falso (D47, E1.4)."""

from datetime import UTC, datetime, timedelta

import pytest

from agente.db.modelos import Pagamento
from agente.integracoes.llm_turno import LLMTurnoFalso, LLMTurnoIndisponivel
from agente.tipos import FormaPagamento, MotivoHumano, StatusPagamento
from agente.turno.acoes import Acao
from agente.turno.uma_chamada import (
    SINAIS,
    TENTATIVAS_UMA_CHAMADA,
    ResolucaoTurno,
    SaidaTurno,
    TentativaUmaChamada,
    acoes_permitidas,
    esquema_da_saida,
    montar_entrada,
    previa_numeros,
    resolver_turno,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
REPLY_PIX = "Seu valor em aberto é {divida}.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
REPLY_LINK = "Seu valor com desconto é {valor_final}. Pague e cadastre o cartão: {link_ctn}"


def _bruto(**mudancas) -> dict:
    bruto = {sinal: False for sinal in SINAIS}
    bruto |= {"tom": "neutro", "acao": "INFORM_DEBT", "reply": REPLY_PIX}
    return bruto | mudancas


class _Previa:
    """A prévia de verdade, anotando com quais ações foi chamada."""

    def __init__(self, conversa, politica):
        self.chamadas: list[tuple[Acao, ...]] = []
        self._conversa, self._politica = conversa, politica

    def __call__(self, acoes):
        self.chamadas.append(tuple(acoes))
        return previa_numeros(acoes, self._conversa, self._conversa.cobranca, self._politica, AGORA)


def _resolver(conversa, politica, respostas):
    cobranca = conversa.cobranca
    entrada = montar_entrada(conversa, cobranca, politica, [("CLIENTE", "oi")], ["oi"], 1, AGORA)
    llm = LLMTurnoFalso(respostas=respostas, registrar=True)
    previa = _Previa(conversa, politica)
    resolucao = resolver_turno(llm, entrada, previa, cobranca)
    return resolucao, llm, previa


def test_a_valida_com_inform_debt_sai_aprovada_em_uma_chamada(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto()])

    assert isinstance(resolucao, ResolucaoTurno)
    assert resolucao.reply == REPLY_PIX
    assert resolucao.motivo is None
    assert resolucao.veredito.acao is Acao.INFORM_DEBT
    assert resolucao.veredito.anexos == (Acao.GENERATE_PIX,)
    assert resolucao.saida.acao == "INFORM_DEBT"
    assert resolucao.tentativas == [TentativaUmaChamada("INFORM_DEBT", REPLY_PIX, ())]
    assert len(llm.pedidos) == 1
    assert previa.chamadas == [(Acao.INFORM_DEBT, Acao.GENERATE_PIX)]
    assert TENTATIVAS_UMA_CHAMADA == 2


def test_com_meio_vigente_o_inform_debt_vai_sem_anexo_e_so_com_a_divida(conversa, politica):
    conversa.cobranca.pagamentos.append(
        Pagamento(
            forma=FormaPagamento.PIX,
            valor_centavos=conversa.cobranca.divida_centavos,
            referencia="TX-1",
            status=StatusPagamento.PENDENTE,
            criado_em=AGORA,
            expira_em=AGORA + timedelta(hours=5),
            br_code="000201BR",
        )
    )
    conversa.forma_enviada = FormaPagamento.PIX
    reply = "Seu valor em aberto é {divida}."

    resolucao, _, previa = _resolver(conversa, politica, [_bruto(reply=reply)])

    assert resolucao.reply == reply
    assert resolucao.veredito.acao is Acao.INFORM_DEBT
    assert resolucao.veredito.anexos == ()
    assert previa.chamadas == [(Acao.INFORM_DEBT,)]


def test_com_meio_vigente_o_texto_com_o_pix_e_reprovado(conversa, politica):
    conversa.cobranca.pagamentos.append(
        Pagamento(
            forma=FormaPagamento.PIX,
            valor_centavos=conversa.cobranca.divida_centavos,
            referencia="TX-1",
            status=StatusPagamento.PENDENTE,
            criado_em=AGORA,
            expira_em=AGORA + timedelta(hours=5),
            br_code="000201BR",
        )
    )
    conversa.forma_enviada = FormaPagamento.PIX

    resolucao, _, _ = _resolver(conversa, politica, [_bruto(), _bruto()])

    assert resolucao.reply is None
    assert [t.reprovadas for t in resolucao.tentativas] == [
        ("marcador_sem_valor:pix_copia_e_cola",)
    ] * 2


def test_texto_com_digito_e_reprovado_e_reprovado_de_novo_sai_sem_texto_nem_motivo(
    conversa, politica
):
    ruim = _bruto(reply="O valor é R$ 100 e {divida}.\nPix CNPJ: {pix_copia_e_cola}")

    resolucao, llm, _ = _resolver(conversa, politica, [ruim, ruim])

    assert resolucao.reply is None and resolucao.motivo is None
    assert len(llm.pedidos) == 2
    assert "valor_em_digitos" in llm.pedidos[1][1]
    assert [t.reprovadas for t in resolucao.tentativas] == [("valor_em_digitos",)] * 2
    assert resolucao.saida is not None and resolucao.veredito.acao is Acao.INFORM_DEBT


def test_send_ctn_link_sem_o_marcador_do_link_e_reprovado(conversa, politica):
    sem_link = _bruto(acao="SEND_CTN_LINK", reply="Segue o link, é só clicar e pagar.")

    resolucao, _, _ = _resolver(conversa, politica, [sem_link, sem_link])

    assert resolucao.reply is None
    assert resolucao.tentativas[0].reprovadas == ("marcador_faltando:link_ctn",)


def test_saida_que_nao_valida_levanta_indisponivel_sem_nova_tentativa(conversa, politica):
    incompleta = _bruto()
    del incompleta["tom"]

    with pytest.raises(LLMTurnoIndisponivel, match="não valida"):
        _resolver(conversa, politica, [incompleta, _bruto()])


def test_acao_fora_das_permitidas_levanta_indisponivel_sem_nova_tentativa(conversa, politica):
    conversa.degrau_atual = 3  # o último degrau: não há mais desconto

    with pytest.raises(LLMTurnoIndisponivel, match="fora das permitidas"):
        _resolver(conversa, politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK), _bruto()])


def test_o_esquema_sem_degrau_nao_tem_o_desconto_e_e_estrito(conversa, politica):
    conversa.degrau_atual = 3  # o último degrau: não há mais desconto
    permitidas = acoes_permitidas(conversa, conversa.cobranca, politica)

    esquema = esquema_da_saida(permitidas)

    assert "OFFER_DISCOUNT" not in esquema["properties"]["acao"]["enum"]
    assert esquema["properties"]["acao"]["enum"] == [acao.value for acao in permitidas]
    assert esquema["type"] == "object" and esquema["additionalProperties"] is False
    assert esquema["required"] == list(esquema["properties"])
    # As propriedades são as que a saída aceita, nem mais nem menos.
    assert set(esquema["properties"]) == set(SaidaTurno.model_fields)


def test_llm_fora_sem_guarda_levanta_para_as_tentativas_com_espera(conversa, politica):
    with pytest.raises(LLMTurnoIndisponivel, match="o LLM respondeu 500"):
        _resolver(conversa, politica, [LLMTurnoIndisponivel("o LLM respondeu 500")])


def test_llm_fora_na_segunda_tentativa_tambem_levanta(conversa, politica):
    ruim = _bruto(reply="sem marcador nenhum")

    with pytest.raises(LLMTurnoIndisponivel, match="prazo"):
        _resolver(conversa, politica, [ruim, LLMTurnoIndisponivel("prazo")])


def test_o_humano_da_primeira_tentativa_nao_chama_de_novo(conversa, politica):
    resolucao, llm, _ = _resolver(conversa, politica, [_bruto(quer_humano=True)])

    assert resolucao.motivo is MotivoHumano.PEDIU_HUMANO
    assert len(llm.pedidos) == 1


def test_none_sai_sem_reply_nem_motivo_e_sem_chamar_a_previa(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(acao="NONE", reply="")])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.veredito.acao is Acao.NONE
    assert len(llm.pedidos) == 1 and previa.chamadas == []
    assert resolucao.tentativas == [TentativaUmaChamada("NONE", "", ())]


def test_none_com_texto_so_de_espacos_sai_como_o_none_vazio(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(acao="NONE", reply="  \n")])

    assert resolucao.reply is None and resolucao.motivo is None
    assert len(llm.pedidos) == 1 and previa.chamadas == []


def test_o_tom_impaciente_com_none_nao_vai_ao_humano(conversa, politica):
    resolucao, _, _ = _resolver(
        conversa, politica, [_bruto(tom="impaciente", acao="NONE", reply="")]
    )

    assert resolucao.motivo is None


@pytest.mark.parametrize("vazio", ["", "   \n"])
def test_reply_vazio_com_inform_debt_e_reprovado(conversa, politica, vazio):
    resolucao, _, _ = _resolver(conversa, politica, [_bruto(reply=vazio), _bruto(reply=vazio)])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.tentativas[0].reprovadas == ("texto_vazio",)


def test_a_previa_so_roda_com_as_acoes_do_veredito(conversa, politica):
    conversa.cobranca.propenso_cartao = True

    _, _, previa = _resolver(
        conversa, politica, [_bruto(reply="O valor é {divida}. Pague aqui: {link_ctn}")]
    )

    assert previa.chamadas == [(Acao.INFORM_DEBT, Acao.SEND_CTN_LINK)]


def test_a_previa_nao_muda_nada_no_banco(conversa, politica):
    _resolver(conversa, politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK)])

    assert conversa.degrau_atual == 0 and conversa.forma_enviada is None
    assert conversa.cobranca.pagamentos == []


def test_o_como_dict_traz_as_tentativas_a_causa_e_o_caminho(conversa, politica):
    ruim = _bruto(reply="Sem marcador nenhum.")
    resolucao, _, _ = _resolver(conversa, politica, [ruim, _bruto()])

    dados = resolucao.como_dict("INFORM_DEBT")

    assert dados["caminho"] == "uma_chamada"
    assert dados["saida"] == "INFORM_DEBT"
    assert dados["causa"] == "acao"
    assert dados["tentativas"] == [
        {
            "texto": "Sem marcador nenhum.",
            "acao": "INFORM_DEBT",
            "reprovadas": ["marcador_faltando:divida", "marcador_faltando:pix_copia_e_cola"],
        },
        {"texto": REPLY_PIX, "acao": "INFORM_DEBT", "reprovadas": []},
    ]
    assert "erro" not in dados


def test_alega_pagamento_com_reply_nulo_vai_a_conciliacao_sem_falhar(conversa, politica):
    saida = _bruto(alega_pagamento=True, acao=None, reply=None)

    resolucao, llm, _ = _resolver(conversa, politica, [saida])

    assert resolucao.motivo is MotivoHumano.CONCILIACAO
    assert resolucao.veredito.causa == "sinal:alega_pagamento"
    assert len(llm.pedidos) == 1
