"""A saída do turno de uma chamada, a guarda por palavra-chave e a decisão (D47, E1.3)."""

import pytest

from agente.tipos import Laudo, MotivoHumano
from agente.turno.acoes import Acao
from agente.turno.sinais import GATILHOS
from agente.turno.uma_chamada import (
    MAX_HANDOFF_REPLY,
    MOTIVO_DO_SINAL,
    ORIGEM_UMA_CHAMADA,
    PALAVRAS_CHAVE,
    SINAIS,
    SINAIS_DE_SAIDA,
    SaidaTurno,
    Veredito,
    decidir,
    ler_saida,
    palavra_chave,
    texto_de_handoff,
)

PERMITIDAS = (
    Acao.INFORM_DEBT,
    Acao.OFFER_DISCOUNT,
    Acao.SEND_CTN_LINK,
    Acao.GENERATE_PIX,
    Acao.OUT_OF_SCOPE,
    Acao.NONE,
)
SEM_DESCONTO = tuple(a for a in PERMITIDAS if a is not Acao.OFFER_DISCOUNT)


def _bruto(**mudancas) -> dict:
    bruto = {sinal: False for sinal in SINAIS}
    bruto |= {
        "tom": "neutro",
        "acao": "INFORM_DEBT",
        "reply": "Oi! O valor é {divida}.",
        "handoff_reply": "",
    }
    return bruto | mudancas


def _saida(**mudancas) -> SaidaTurno:
    return ler_saida(_bruto(**mudancas))


def _decidir(cobranca, permitidas=PERMITIDAS, guarda=None, **mudancas) -> Veredito:
    return decidir(_saida(**mudancas), permitidas, guarda, cobranca)


# ler_saida e SaidaTurno


def test_as_constantes_e_o_inverso_do_gatilhos():
    assert ORIGEM_UMA_CHAMADA == "luna:uma-chamada"
    assert MAX_HANDOFF_REPLY == 300
    assert MOTIVO_DO_SINAL == {
        **{sinal: motivo for motivo, sinal in GATILHOS.items()},
        "quer_humano": MotivoHumano.PEDIU_HUMANO,
    }
    assert MotivoHumano.PEDIU_HUMANO.value == "PEDIU_HUMANO"


def test_a_saida_valida_e_lida():
    saida = _saida(desconfia_golpe=True, tom="impaciente", handoff_reply="Entendi.")
    assert saida.desconfia_golpe is True and saida.risco_juridico is False
    assert (saida.tom, saida.acao, saida.reply, saida.handoff_reply) == (
        "impaciente",
        "INFORM_DEBT",
        "Oi! O valor é {divida}.",
        "Entendi.",
    )


def test_campo_a_mais_e_ignorado():
    assert _saida(extra="qualquer coisa").acao == "INFORM_DEBT"


@pytest.mark.parametrize("campo", [*SINAIS, "tom", "acao", "reply"])
def test_campo_faltando_levanta_erro(campo):
    bruto = _bruto()
    del bruto[campo]
    with pytest.raises(ValueError):
        ler_saida(bruto)


def test_handoff_reply_faltando_e_aceito():
    bruto = _bruto()
    del bruto["handoff_reply"]
    assert ler_saida(bruto).handoff_reply is None


def test_tom_cordial_levanta_erro():
    with pytest.raises(ValueError):
        ler_saida(_bruto(tom="cordial"))


@pytest.mark.parametrize("valor", ["true", "sim", 1, None])
def test_sinal_que_nao_e_booleano_levanta_erro(valor):
    with pytest.raises(ValueError):
        ler_saida(_bruto(terceiro=valor))


def test_acao_que_nao_e_texto_ou_reply_grande_demais_levanta_erro():
    with pytest.raises(ValueError):
        ler_saida(_bruto(acao=3))
    with pytest.raises(ValueError):
        ler_saida(_bruto(reply="x" * 1001))
    assert ler_saida(_bruto(reply="x" * 1000)).reply == "x" * 1000


def test_handoff_reply_como_lista_e_aceito_mas_nao_vira_texto():
    saida = _saida(handoff_reply=["a", "b"])
    assert texto_de_handoff(saida) is None


def test_a_leitura_traz_os_sinais_e_o_tom_como_laudos():
    leitura = _saida(risco_juridico=True, tom="hostil").leitura()
    assert leitura.sim("risco_juridico") is True
    assert leitura.sim("terceiro") is False
    assert leitura.tom == "hostil"
    assert leitura.laudo("quer_humano") == Laudo("quer_humano", False, 1.0, 1.0)
    assert [laudo.pergunta for laudo in leitura.laudos] == [*SINAIS, "tom"]
    assert {(laudo.probabilidade, laudo.confianca) for laudo in leitura.laudos} == {(1.0, 1.0)}


def test_a_leitura_com_a_guarda_do_golpe_vale_sim_mesmo_com_o_sinal_falso():
    saida = _saida()
    assert saida.leitura().sim("desconfia_golpe") is False
    assert saida.leitura(golpe_pela_guarda=True).sim("desconfia_golpe") is True
    assert _saida(desconfia_golpe=True).leitura().sim("desconfia_golpe") is True


def test_o_laudo_da_acao():
    assert _saida(acao="OFFER_DISCOUNT").laudo_da_acao() == Laudo(
        "acao", "OFFER_DISCOUNT", 1.0, 1.0
    )


# a guarda


def test_a_ordem_das_palavras_chave():
    assert [motivo for _, motivo in PALAVRAS_CHAVE] == [
        MotivoHumano.DESCONFIANCA,
        MotivoHumano.JURIDICO,
        MotivoHumano.PEDIU_HUMANO,
    ]


@pytest.mark.parametrize(
    "texto, motivo, palavra",
    [
        ("Isso é golpe?", MotivoHumano.DESCONFIANCA, "golpe"),
        ("vocês são golpistas", MotivoHumano.DESCONFIANCA, "golpistas"),
        ("vou no Procon", MotivoHumano.JURIDICO, "procon"),
        ("vou chamar meu advogado", MotivoHumano.JURIDICO, "advogado"),
        ("a advogada vai falar", MotivoHumano.JURIDICO, "advogada"),
        ("vou processar vocês", MotivoHumano.JURIDICO, "processar"),
        ("vou abrir um processo", MotivoHumano.JURIDICO, "processo"),
        ("Quero falar com um ATENDENTE", MotivoHumano.PEDIU_HUMANO, "atendente"),
        ("quero um humano", MotivoHumano.PEDIU_HUMANO, "humano"),
        ("preciso falar com alguém", MotivoHumano.PEDIU_HUMANO, "falar com alguem"),
        ("vou no Procon, isso é golpe", MotivoHumano.DESCONFIANCA, "golpe"),
        ("quero um humano, vou no procon", MotivoHumano.JURIDICO, "procon"),
        # O falso positivo medido na decisão 15: vai ao humano, que é o lado seguro.
        ("como é o processo de pagamento?", MotivoHumano.JURIDICO, "processo"),
    ],
)
def test_a_palavra_chave_acha_o_motivo(texto, motivo, palavra):
    assert palavra_chave([texto]) == (motivo, palavra)


@pytest.mark.parametrize(
    "texto", ["processamento", "Bom dia", "quanto eu devo?", "o cartão está desumano", ""]
)
def test_a_palavra_chave_nao_pega_o_que_nao_e(texto):
    assert palavra_chave([texto]) is None


def test_a_palavra_chave_olha_todos_os_textos_e_a_lista_vazia_nao_acha():
    assert palavra_chave(["bom dia", "meu advogado"]) == (MotivoHumano.JURIDICO, "advogado")
    assert palavra_chave(["meu advogado", "isso é golpe"])[0] is MotivoHumano.DESCONFIANCA
    assert palavra_chave([]) is None


# a decisão


def test_a_guarda_vence_a_acao(conversa):
    guarda = palavra_chave(["Isso é golpe?"])
    veredito = _decidir(conversa.cobranca, guarda=guarda, acao="INFORM_DEBT")
    assert veredito == Veredito(MotivoHumano.DESCONFIANCA, None, (), False, "palavra_chave:golpe")


def test_a_guarda_vence_o_sinal(conversa):
    guarda = palavra_chave(["vou no procon"])
    veredito = _decidir(conversa.cobranca, guarda=guarda, quer_retencao=True)
    assert veredito.motivo is MotivoHumano.JURIDICO
    assert veredito.causa == "palavra_chave:procon"


@pytest.mark.parametrize("sinal, motivo", [(s, m) for m, s in GATILHOS.items()])
def test_cada_sinal_de_saida_leva_ao_seu_motivo(conversa, sinal, motivo):
    veredito = _decidir(conversa.cobranca, **{sinal: True})
    assert veredito == Veredito(motivo, None, (), False, f"sinal:{sinal}")


def test_os_sinais_de_saida_seguem_a_ordem_do_gatilhos(conversa):
    # quer_retencao vem antes de desconfia_golpe no GATILHOS.
    veredito = _decidir(conversa.cobranca, quer_retencao=True, desconfia_golpe=True)
    assert veredito.motivo is MotivoHumano.RETENCAO
    assert SINAIS_DE_SAIDA.index("quer_retencao") < SINAIS_DE_SAIDA.index("desconfia_golpe")


def test_o_sinal_vence_o_tom_e_o_tom_vence_o_quer_humano(conversa):
    cobranca = conversa.cobranca
    assert _decidir(cobranca, tom="hostil", vulnerabilidade=True).motivo is MotivoHumano.CAUTELA
    tom = _decidir(cobranca, tom="irritado", quer_humano=True)
    assert (tom.motivo, tom.causa) == (MotivoHumano.TOM, "tom:irritado")
    assert _decidir(cobranca, tom="hostil").causa == "tom:hostil"


def test_o_quer_humano_leva_ao_pediu_humano(conversa):
    veredito = _decidir(conversa.cobranca, quer_humano=True, terceiro=True)
    assert veredito == Veredito(MotivoHumano.PEDIU_HUMANO, None, (), False, "sinal:quer_humano")


def test_impaciente_nao_sai(conversa):
    veredito = _decidir(conversa.cobranca, tom="impaciente")
    assert veredito.motivo is None and veredito.acao is Acao.INFORM_DEBT


def test_o_terceiro_vence_a_acao(conversa):
    veredito = _decidir(conversa.cobranca, terceiro=True, acao="SEND_CTN_LINK")
    assert veredito == Veredito(None, None, (), True, "sinal:terceiro")


@pytest.mark.parametrize("valor", ["OFFER_DISCOUNT", "HUMAN", "XYZ", "", "inform_debt"])
def test_acao_fora_das_permitidas_nao_vira_motivo_nem_acao(conversa, valor):
    veredito = _decidir(conversa.cobranca, SEM_DESCONTO, acao=valor)
    assert veredito == Veredito(None, None, (), False, f"acao_fora:{valor}")


def test_inform_debt_leva_o_link_de_quem_e_propenso_e_o_pix_de_quem_nao_e(conversa):
    cobranca = conversa.cobranca
    cobranca.propenso_cartao = True
    veredito = _decidir(cobranca, acao="INFORM_DEBT")
    assert veredito == Veredito(None, Acao.INFORM_DEBT, (Acao.SEND_CTN_LINK,), False, "acao")
    cobranca.propenso_cartao = False
    assert _decidir(cobranca, acao="INFORM_DEBT").anexos == (Acao.GENERATE_PIX,)


def test_offer_discount_leva_o_link_e_none_vai_sem_anexos(conversa):
    cobranca = conversa.cobranca
    assert _decidir(cobranca, acao="OFFER_DISCOUNT").anexos == (Acao.SEND_CTN_LINK,)
    veredito = _decidir(cobranca, acao="NONE")
    assert (veredito.acao, veredito.anexos, veredito.motivo) == (Acao.NONE, (), None)
    assert _decidir(cobranca, acao="GENERATE_PIX").anexos == ()


# texto_de_handoff


def test_o_texto_de_handoff_valido_passa_sem_os_espacos_das_pontas():
    texto = "  Entendi, o Procon. Passei a conversa para a nossa equipe, que segue por aqui.\n"
    assert texto_de_handoff(_saida(handoff_reply=texto)) == texto.strip()


@pytest.mark.parametrize(
    "texto",
    [
        "Passei para a equipe em 2 minutos.",
        "Veja www.exemplo.com",
        "Veja https://x.invalid",
        "Fica {divida} pela equipe.",
        "Fica R$ pela equipe.",
        "x" * 301,
        "",
        "   ",
        None,
        42,
    ],
)
def test_o_texto_de_handoff_invalido_da_none(texto):
    assert texto_de_handoff(_saida(handoff_reply=texto)) is None


def test_o_texto_de_handoff_de_300_caracteres_passa_e_sem_saida_da_none():
    assert texto_de_handoff(_saida(handoff_reply="x" * 300)) == "x" * 300
    assert texto_de_handoff(None) is None


@pytest.mark.parametrize("campo", ["acao", "reply"])
def test_acao_e_reply_nulos_sao_lidos_como_vazios(campo):
    saida = ler_saida(_bruto(**{campo: None}))

    assert getattr(saida, campo) == ""


def test_o_sinal_vale_com_a_acao_e_o_reply_nulos(conversa):
    veredito = _decidir(conversa.cobranca, alega_pagamento=True, acao=None, reply=None)

    assert veredito.motivo is MotivoHumano.CONCILIACAO
    assert veredito.causa == "sinal:alega_pagamento"


def test_a_acao_nula_sem_sinal_e_acao_fora(conversa):
    veredito = _decidir(conversa.cobranca, acao=None)

    assert (veredito.motivo, veredito.acao) == (None, None)
    assert veredito.causa == "acao_fora:"
