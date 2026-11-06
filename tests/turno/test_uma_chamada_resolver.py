"""O laço das tentativas do turno de uma chamada, com o LLM falso (D47, E1.4)."""

from datetime import UTC, datetime

import pytest

from agente.integracoes.llm_turno import JSONInvalido, LLMTurnoFalso, LLMTurnoIndisponivel
from agente.tipos import MotivoHumano
from agente.turno.acoes import Acao
from agente.turno.uma_chamada import (
    SINAIS,
    TENTATIVAS_UMA_CHAMADA,
    ResolucaoTurno,
    TentativaUmaChamada,
    montar_entrada,
    palavra_chave,
    previa_numeros,
    resolver_turno,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
REPLY_PIX = "Seu valor em aberto é {divida}. Para pagar no Pix, use o código: {pix_copia_e_cola}"
REPLY_LINK = "Seu valor com desconto é {valor_final}. Pague e cadastre o cartão: {link_ctn}"
HANDOFF = "Entendi a sua preocupação com o golpe. Passei a conversa para a nossa equipe."


def _bruto(**mudancas) -> dict:
    bruto = {sinal: False for sinal in SINAIS}
    bruto |= {"tom": "neutro", "acao": "INFORM_DEBT", "reply": REPLY_PIX, "handoff_reply": ""}
    return bruto | mudancas


class _Previa:
    """A prévia de verdade, anotando com quais ações foi chamada."""

    def __init__(self, conversa, politica):
        self.chamadas: list[tuple[Acao, ...]] = []
        self._conversa, self._politica = conversa, politica

    def __call__(self, acoes):
        self.chamadas.append(tuple(acoes))
        return previa_numeros(acoes, self._conversa, self._conversa.cobranca, self._politica, AGORA)


def _resolver(conversa, politica, respostas, guarda=None):
    cobranca = conversa.cobranca
    entrada = montar_entrada(conversa, cobranca, politica, [("CLIENTE", "oi")], ["oi"], 1, AGORA)
    llm = LLMTurnoFalso(respostas=respostas, registrar=True)
    previa = _Previa(conversa, politica)
    resolucao = resolver_turno(llm, entrada, guarda, previa, cobranca)
    return resolucao, llm, previa


def test_a_valida_com_inform_debt_sai_aprovada_em_uma_chamada(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto()])

    assert isinstance(resolucao, ResolucaoTurno)
    assert resolucao.reply == REPLY_PIX
    assert resolucao.motivo is None and resolucao.erro is None
    assert resolucao.veredito.acao is Acao.INFORM_DEBT
    assert resolucao.veredito.anexos == (Acao.GENERATE_PIX,)
    assert resolucao.saida.acao == "INFORM_DEBT"
    assert resolucao.tentativas == [TentativaUmaChamada("INFORM_DEBT", REPLY_PIX, ())]
    assert len(llm.pedidos) == 1
    assert previa.chamadas == [(Acao.INFORM_DEBT, Acao.GENERATE_PIX)]
    assert TENTATIVAS_UMA_CHAMADA == 2


def test_acao_fora_leva_o_motivo_na_segunda_chamada_e_sai_aprovada(conversa, politica):
    conversa.degrau_atual = 3  # o último degrau: não há mais desconto

    resolucao, llm, previa = _resolver(
        conversa, politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK), _bruto()]
    )

    assert len(llm.pedidos) == 2
    assert "acao_nao_permitida:OFFER_DISCOUNT" not in llm.pedidos[0][1]
    assert "acao_nao_permitida:OFFER_DISCOUNT" in llm.pedidos[1][1]
    assert resolucao.reply == REPLY_PIX and resolucao.motivo is None
    assert [t.reprovadas for t in resolucao.tentativas] == [
        ("acao_nao_permitida:OFFER_DISCOUNT",),
        (),
    ]
    assert resolucao.tentativas[0].acao == "OFFER_DISCOUNT"
    # A prévia só roda para a ação que passou da decisão.
    assert previa.chamadas == [(Acao.INFORM_DEBT, Acao.GENERATE_PIX)]


def test_texto_com_digito_e_reprovado_e_reprovado_de_novo_sai_sem_texto_nem_motivo(
    conversa, politica
):
    ruim = _bruto(reply="O valor é R$ 100 e {divida}. Pix: {pix_copia_e_cola}")

    resolucao, llm, _ = _resolver(conversa, politica, [ruim, ruim])

    assert resolucao.reply is None and resolucao.motivo is None and resolucao.erro is None
    assert len(llm.pedidos) == 2
    assert "valor_em_digitos" in llm.pedidos[1][1]
    assert [t.reprovadas for t in resolucao.tentativas] == [("valor_em_digitos",)] * 2
    assert resolucao.saida is not None and resolucao.veredito.acao is Acao.INFORM_DEBT


def test_send_ctn_link_sem_o_marcador_do_link_e_reprovado(conversa, politica):
    sem_link = _bruto(acao="SEND_CTN_LINK", reply="Segue o link, é só clicar e pagar.")

    resolucao, _, _ = _resolver(conversa, politica, [sem_link, sem_link])

    assert resolucao.reply is None
    assert resolucao.tentativas[0].reprovadas == ("marcador_faltando:link_ctn",)


def test_json_invalido_conta_como_tentativa_e_a_segunda_valida_sai_aprovada(conversa, politica):
    resolucao, llm, _ = _resolver(conversa, politica, ["isto não é json", _bruto()])

    assert len(llm.pedidos) == 2
    assert "json_invalido" in llm.pedidos[1][1]
    assert resolucao.tentativas[0] == TentativaUmaChamada(None, None, ("json_invalido",))
    assert resolucao.reply == REPLY_PIX


def test_saida_com_campo_faltando_tambem_e_json_invalido(conversa, politica):
    incompleta = _bruto()
    del incompleta["tom"]

    resolucao, _, _ = _resolver(conversa, politica, [incompleta, JSONInvalido("x")])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.saida is None and resolucao.veredito is None
    assert [t.reprovadas for t in resolucao.tentativas] == [("json_invalido",)] * 2


def test_llm_fora_sem_guarda_vai_a_redator_indisponivel_em_uma_chamada(conversa, politica):
    resolucao, llm, _ = _resolver(conversa, politica, [LLMTurnoIndisponivel("o LLM respondeu 500")])

    assert resolucao.motivo is MotivoHumano.REDATOR_INDISPONIVEL
    assert resolucao.erro == "o LLM respondeu 500"
    assert resolucao.reply is None and resolucao.texto_handoff is None
    assert len(llm.pedidos) == 1


def test_llm_fora_na_segunda_tentativa_tambem_e_indisponivel(conversa, politica):
    ruim = _bruto(reply="sem marcador nenhum")

    resolucao, llm, _ = _resolver(conversa, politica, [ruim, LLMTurnoIndisponivel("prazo")])

    assert resolucao.motivo is MotivoHumano.REDATOR_INDISPONIVEL
    assert len(llm.pedidos) == 2
    assert len(resolucao.tentativas) == 1


def test_llm_fora_com_a_guarda_sai_o_motivo_da_guarda_sem_texto(conversa, politica):
    guarda = palavra_chave(["isso é golpe?"])

    resolucao, llm, _ = _resolver(conversa, politica, [LLMTurnoIndisponivel("fora")], guarda=guarda)

    assert resolucao.motivo is MotivoHumano.DESCONFIANCA
    assert resolucao.texto_handoff is None and resolucao.reply is None
    assert resolucao.veredito.causa == "palavra_chave:golpe"
    assert resolucao.erro == "fora"
    assert len(llm.pedidos) == 1


def test_guarda_com_o_json_invalido_sai_o_motivo_da_guarda_em_uma_chamada(conversa, politica):
    guarda = palavra_chave(["vou no procon"])

    resolucao, llm, _ = _resolver(conversa, politica, ["lixo"], guarda=guarda)

    assert resolucao.motivo is MotivoHumano.JURIDICO
    assert resolucao.texto_handoff is None
    assert len(llm.pedidos) == 1


def test_o_handoff_grava_o_handoff_reply_bruto_na_redacao_do_turno(conversa, politica):
    guarda = palavra_chave(["isso é golpe?"])
    saida = _bruto(acao="NONE", reply="", handoff_reply="  ")

    resolucao, _, _ = _resolver(conversa, politica, [saida], guarda=guarda)

    # Vazio ou descartado, o que o LLM deu fica gravado: sem isso não se sabe por que a fixa saiu.
    assert resolucao.texto_handoff is None
    assert resolucao.como_dict("HANDOFF")["tentativas"][0]["handoff_reply"] == "  "


def test_sem_handoff_o_turno_nao_grava_a_chave_handoff_reply(conversa, politica):
    resolucao, _, _ = _resolver(conversa, politica, [_bruto()])

    assert "handoff_reply" not in resolucao.como_dict("LLM")["tentativas"][0]


def test_guarda_com_o_llm_no_ar_leva_o_texto_do_handoff_mesmo_com_a_acao_invalida(
    conversa, politica
):
    guarda = palavra_chave(["isso é golpe?"])
    saida = _bruto(acao="HUMAN", reply="", handoff_reply=f"  {HANDOFF}  ")

    resolucao, llm, previa = _resolver(conversa, politica, [saida, _bruto()], guarda=guarda)

    assert resolucao.motivo is MotivoHumano.DESCONFIANCA
    assert resolucao.texto_handoff == HANDOFF
    assert resolucao.reply is None
    assert len(llm.pedidos) == 1
    assert previa.chamadas == []


def test_handoff_com_texto_invalido_vai_sem_texto(conversa, politica):
    saida = _bruto(risco_juridico=True, handoff_reply="Passei para a equipe em 2 dias.")

    resolucao, _, _ = _resolver(conversa, politica, [saida])

    assert resolucao.motivo is MotivoHumano.JURIDICO
    assert resolucao.texto_handoff is None


def test_o_sinal_que_aparece_so_na_segunda_tentativa_leva_ao_humano(conversa, politica):
    fora = _bruto(acao="XYZ")
    sinal = _bruto(vulnerabilidade=True, handoff_reply=HANDOFF)

    resolucao, llm, _ = _resolver(conversa, politica, [fora, sinal])

    assert resolucao.motivo is MotivoHumano.CAUTELA
    assert resolucao.texto_handoff == HANDOFF
    assert resolucao.veredito.causa == "sinal:vulnerabilidade"
    assert len(llm.pedidos) == 2
    assert resolucao.tentativas[0].reprovadas == ("acao_nao_permitida:XYZ",)


def test_o_humano_da_primeira_tentativa_nao_chama_de_novo(conversa, politica):
    resolucao, llm, _ = _resolver(conversa, politica, [_bruto(quer_humano=True)])

    assert resolucao.motivo is MotivoHumano.PEDIU_HUMANO
    assert len(llm.pedidos) == 1


def test_terceiro_sem_marcador_sai_aprovado(conversa, politica):
    reply = "Oi! Esta conversa é com outra pessoa, então não posso ajudar por aqui."

    resolucao, llm, previa = _resolver(
        conversa, politica, [_bruto(terceiro=True, acao="NONE", reply=reply)]
    )

    assert resolucao.reply == reply and resolucao.motivo is None
    assert resolucao.veredito.terceiro is True
    assert len(llm.pedidos) == 1
    assert previa.chamadas == [()]


def test_terceiro_com_marcador_e_reprovado(conversa, politica):
    com_marcador = _bruto(terceiro=True, reply="Oi! A dívida de {divida} é do titular.")

    resolucao, llm, _ = _resolver(conversa, politica, [com_marcador, com_marcador])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.tentativas[0].reprovadas == ("marcador_com_terceiro",)
    assert "marcador_com_terceiro" in llm.pedidos[1][1]
    assert resolucao.veredito.terceiro is True


def test_terceiro_com_valor_no_texto_e_reprovado_pela_previa(conversa, politica):
    com_valor = _bruto(terceiro=True, reply="Oi! São R$ 100 para o titular.")

    resolucao, _, _ = _resolver(conversa, politica, [com_valor, com_valor])

    assert resolucao.reply is None
    assert resolucao.tentativas[0].reprovadas == ("valor_em_digitos",)


def test_terceiro_com_reply_vazio_retorna_como_none(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(terceiro=True, reply=" ")])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.veredito.terceiro is True and resolucao.veredito.acao is Acao.NONE
    assert len(llm.pedidos) == 1 and previa.chamadas == []


def test_none_sai_sem_reply_nem_motivo_e_sem_chamar_a_previa(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(acao="NONE", reply="")])

    assert resolucao.reply is None and resolucao.motivo is None and resolucao.erro is None
    assert resolucao.veredito.acao is Acao.NONE
    assert len(llm.pedidos) == 1 and previa.chamadas == []
    assert resolucao.tentativas == [TentativaUmaChamada("NONE", "", ())]


ACOLHIMENTO = "Poxa, sinto muito que esteja assim. O que aconteceu? Me conta como posso ajudar."


def test_none_com_texto_sem_marcador_sai_aprovado_sem_motivo(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(acao="NONE", reply=ACOLHIMENTO)])

    assert resolucao.reply == ACOLHIMENTO and resolucao.motivo is None
    assert resolucao.veredito.acao is Acao.NONE and resolucao.veredito.terceiro is False
    assert resolucao.tentativas == [TentativaUmaChamada("NONE", ACOLHIMENTO, ())]
    assert len(llm.pedidos) == 1
    # A prévia roda sem nenhuma ação: não há número nem link para o texto usar.
    assert previa.chamadas == [()]


@pytest.mark.parametrize(
    ("texto", "motivo"),
    [
        ("Sinto muito. Sua dívida é {divida}. O que houve?", "marcador_com_none"),
        ("Sinto muito. Qual é o problema? Pague aqui: {link_ctn}", "marcador_com_none"),
        ("Sinto muito. Faz 3 dias que você sumiu?", "valor_em_digitos"),
        ("Sinto muito. Veja https://exemplo.com.br/x e me conte.", "link_nao_autorizado"),
    ],
)
def test_none_com_marcador_digito_ou_link_e_reprovado_e_vai_a_segunda_tentativa(
    conversa, politica, texto, motivo
):
    reprovada = _bruto(acao="NONE", reply=texto)
    aprovada = _bruto(acao="NONE", reply=ACOLHIMENTO)

    resolucao, llm, _ = _resolver(conversa, politica, [reprovada, aprovada])

    assert len(llm.pedidos) == 2
    assert resolucao.tentativas[0].reprovadas[0] == motivo
    assert motivo in llm.pedidos[1][1]
    assert resolucao.reply == ACOLHIMENTO and resolucao.tentativas[1].reprovadas == ()


def test_none_reprovado_duas_vezes_sai_sem_texto_nem_motivo(conversa, politica):
    reprovada = _bruto(acao="NONE", reply="Sinto muito. Sua dívida é {divida}.")

    resolucao, _, _ = _resolver(conversa, politica, [reprovada, reprovada])

    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.veredito.acao is Acao.NONE
    assert all(t.reprovadas for t in resolucao.tentativas)


def test_none_vazio_depois_de_texto_reprovado_conta_como_reprovado_e_nao_como_silencio(
    conversa, politica
):
    # M-a: o acolhimento reprovou e a 2a tentativa devolveu NONE vazio. Sem isto o turno saia
    # como "nada a enviar" e o cliente ficava sem resposta.
    reprovada = _bruto(acao="NONE", reply="Sinto muito. Sua dívida é {divida}.")
    vazia = _bruto(acao="NONE", reply="")

    resolucao, llm, _ = _resolver(conversa, politica, [reprovada, vazia])

    assert len(llm.pedidos) == 2
    assert resolucao.reply is None and resolucao.motivo is None
    assert resolucao.veredito.acao is Acao.NONE
    assert resolucao.tentativas[-1].reprovadas == ("texto_vazio",)


def test_none_vazio_depois_de_texto_de_acao_reprovado_tambem_nao_e_silencio(conversa, politica):
    ruim = _bruto(reply="O valor é R$ 100 e {divida}. Pix: {pix_copia_e_cola}")
    vazia = _bruto(acao="NONE", reply="")

    resolucao, _, _ = _resolver(conversa, politica, [ruim, vazia])

    assert resolucao.reply is None
    assert resolucao.tentativas[-1].reprovadas == ("texto_vazio",)


def test_none_vazio_depois_de_json_invalido_ou_acao_fora_segue_como_silencio(conversa, politica):
    resolucao, _, _ = _resolver(
        conversa, politica, ["isto não é json", _bruto(acao="NONE", reply="")]
    )

    assert resolucao.tentativas[-1] == TentativaUmaChamada("NONE", "", ())

    fora = _bruto(acao="FOO", reply="algo")
    resolucao, _, _ = _resolver(conversa, politica, [fora, _bruto(acao="NONE", reply="")])

    assert resolucao.tentativas[-1] == TentativaUmaChamada("NONE", "", ())


def test_none_com_texto_so_de_espacos_sai_como_o_none_vazio(conversa, politica):
    resolucao, llm, previa = _resolver(conversa, politica, [_bruto(acao="NONE", reply="  \n")])

    assert resolucao.reply is None and resolucao.motivo is None
    assert len(llm.pedidos) == 1 and previa.chamadas == []


def test_o_tom_impaciente_com_none_e_texto_nao_vai_ao_humano(conversa, politica):
    resolucao, _, _ = _resolver(
        conversa, politica, [_bruto(tom="impaciente", acao="NONE", reply=ACOLHIMENTO)]
    )

    assert resolucao.motivo is None and resolucao.reply == ACOLHIMENTO


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


def test_o_como_dict_com_erro_e_sem_veredito(conversa, politica):
    resolucao, _, _ = _resolver(conversa, politica, [LLMTurnoIndisponivel("fora")])

    dados = resolucao.como_dict("HUMAN:REDATOR_INDISPONIVEL")

    assert dados["erro"] == "fora" and dados["tentativas"] == []
    assert "causa" not in dados and dados["caminho"] == "uma_chamada"


def test_guarda_com_o_json_invalido_anexa_a_tentativa_e_o_motivo(conversa, politica):
    guarda = palavra_chave(["vou no procon"])

    resolucao, _, _ = _resolver(conversa, politica, ["lixo"], guarda=guarda)

    assert resolucao.motivo is MotivoHumano.JURIDICO
    assert resolucao.tentativas == [TentativaUmaChamada(None, None, ("json_invalido",))]


def test_alega_pagamento_com_reply_nulo_vai_a_conciliacao_e_nao_ao_json_invalido(
    conversa, politica
):
    saida = _bruto(alega_pagamento=True, acao=None, reply=None)

    resolucao, llm, _ = _resolver(conversa, politica, [saida])

    assert resolucao.motivo is MotivoHumano.CONCILIACAO
    assert resolucao.veredito.causa == "sinal:alega_pagamento"
    assert len(llm.pedidos) == 1
