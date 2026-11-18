"""A entrada do turno de uma chamada: a política, as ações permitidas e a prévia (D47, E1.2)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agente.db.modelos import Pagamento
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.pix import PixFalso
from agente.politica import Regua
from agente.tipos import FormaPagamento, StatusPagamento
from agente.turno.acoes import Acao, anexar
from agente.turno.catalogo import TEXTO_DO_PIX
from agente.turno.executar import executar
from agente.turno.redacao import MARCADORES, marcadores_do_turno
from agente.turno.sinais import CRITERIOS_DA_ACAO, DESCRICAO_ACOES, GATILHOS, SINAIS_DO_PROMPT
from agente.turno.uma_chamada import (
    _FAZ_NONE,
    GLOSSARIO_MARCADORES,
    LINK_DE_PREVIA,
    ORIGEM_UMA_CHAMADA,
    POLITICA_COBRANCA,
    SINAIS,
    SINAIS_DE_SAIDA,
    SISTEMA,
    TONS_UMA_CHAMADA,
    EntradaTurno,
    acoes_permitidas,
    esquema_da_saida,
    montar_entrada,
    montar_mensagens,
    previa_numeros,
)

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
HISTORICO = [("ABERTURA", "[abertura]"), ("CLIENTE", "quanto eu devo?")]
TODAS = (
    Acao.INFORM_DEBT,
    Acao.OFFER_DISCOUNT,
    Acao.SEND_CTN_LINK,
    Acao.GENERATE_PIX,
    Acao.ANSWER,
    Acao.OUT_OF_SCOPE,
    Acao.NONE,
)


def _deps(politica):
    ctn = CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)])
    return SimpleNamespace(politica=politica, ctn=ctn, pix=PixFalso(agora=lambda: AGORA))


def _entrada(conversa, politica, *, novas=("e o desconto?",), usadas=1):
    return montar_entrada(
        conversa, conversa.cobranca, politica, HISTORICO, list(novas), usadas, AGORA
    )


def test_as_constantes_acompanham_os_gatilhos():
    assert ORIGEM_UMA_CHAMADA == "luna:uma-chamada"
    assert TONS_UMA_CHAMADA == ("neutro", "impaciente", "irritado", "hostil")
    assert SINAIS_DE_SAIDA == tuple(GATILHOS.values())
    assert SINAIS == (*SINAIS_DE_SAIDA, "quer_humano", "terceiro", "sem_resposta")
    assert len(SINAIS) == 11


# acoes_permitidas


def test_r3_no_degrau_zero_pode_oferecer_desconto(conversa, politica):
    permitidas = acoes_permitidas(conversa, conversa.cobranca, politica)
    assert permitidas == TODAS


def test_r3_no_ultimo_degrau_nao_oferece_desconto(conversa, politica):
    conversa.degrau_atual = 3
    permitidas = acoes_permitidas(conversa, conversa.cobranca, politica)
    assert Acao.OFFER_DISCOUNT not in permitidas
    assert permitidas == tuple(a for a in TODAS if a is not Acao.OFFER_DISCOUNT)


def test_r1_sem_desconto_nao_oferece_desconto(conversa, politica):
    conversa.cobranca.regua = Regua.R1
    assert Acao.OFFER_DISCOUNT not in acoes_permitidas(conversa, conversa.cobranca, politica)


def test_a_answer_vem_depois_do_pix_e_antes_do_fora_do_assunto(conversa, politica):
    permitidas = acoes_permitidas(conversa, conversa.cobranca, politica)
    assert permitidas[-3:] == (Acao.ANSWER, Acao.OUT_OF_SCOPE, Acao.NONE)
    assert permitidas[permitidas.index(Acao.GENERATE_PIX) + 1] is Acao.ANSWER


def test_o_humano_nunca_e_uma_acao_permitida(conversa, politica):
    for regua in Regua:
        conversa.cobranca.regua = regua
        assert Acao.HUMAN not in acoes_permitidas(conversa, conversa.cobranca, politica)


# previa_numeros


@pytest.mark.parametrize("propenso", [False, True])
@pytest.mark.parametrize("acao", TODAS)
def test_a_previa_bate_com_o_executar(conversa, politica, acao, propenso):
    conversa.cobranca.propenso_cartao = propenso
    acoes = [acao, *anexar(acao, conversa.cobranca, tem_meio_vigente=False)]

    previa = previa_numeros(acoes, conversa, conversa.cobranca, politica, AGORA)
    real = executar(acoes, conversa.cobranca, conversa, _deps(politica), AGORA)

    assert marcadores_do_turno(previa) == marcadores_do_turno(real)
    assert previa.acoes == real.acoes
    assert previa.degrau == real.degrau
    assert previa.valor_final_centavos == real.valor_final_centavos
    assert previa.desconto_centavos == real.desconto_centavos
    assert previa.forma == real.forma
    assert previa.divida_centavos == real.divida_centavos
    assert previa.mensalidades == real.mensalidades


def test_a_previa_nao_tem_efeito_nenhum(conversa, politica):
    deps = _deps(politica)
    cobranca = conversa.cobranca
    conversa.degrau_atual = 1
    acoes = [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK, Acao.GENERATE_PIX]

    previa_numeros(acoes, conversa, cobranca, politica, AGORA)

    assert conversa.degrau_atual == 1
    assert conversa.forma_enviada is None
    assert cobranca.pagamentos == []
    assert deps.ctn.links == {} and deps.pix.cobrancas == {}


def test_a_previa_traz_os_valores_de_mentira(conversa, politica):
    conversa.degrau_atual = 1
    acoes = [Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK]
    link = previa_numeros(acoes, conversa, conversa.cobranca, politica, AGORA)
    assert link.degrau == 2
    assert link.link_ctn == LINK_DE_PREVIA == "https://previa.invalid/link"
    assert link.forma is FormaPagamento.LINK_CTN
    assert link.br_code is None

    pix = previa_numeros([Acao.GENERATE_PIX], conversa, conversa.cobranca, politica, AGORA)
    assert pix.br_code == "PREVIA"
    assert pix.valor_pix_centavos == conversa.cobranca.divida_centavos
    assert pix.forma is FormaPagamento.PIX
    assert pix.link_ctn is None

    sem = previa_numeros([Acao.NONE], conversa, conversa.cobranca, politica, AGORA)
    assert sem.forma is None and sem.link_ctn is None and sem.br_code is None


def test_a_previa_sem_degrau_seguinte_recusa_como_o_executar(conversa, politica):
    conversa.degrau_atual = 3
    with pytest.raises(ValueError, match="degrau"):
        previa_numeros([Acao.OFFER_DISCOUNT], conversa, conversa.cobranca, politica, AGORA)


# montar_entrada


def test_o_json_sem_a_conversa_nao_traz_dinheiro_link_nem_pix(conversa, politica):
    entrada = _entrada(conversa, politica)
    dados = json.loads(entrada.como_json())
    dados.pop("conversa")
    texto = json.dumps(dados, ensure_ascii=False)

    divida = conversa.cobranca.divida_centavos
    assert str(divida) not in texto
    assert formatar_reais(divida) not in texto and "R$" not in texto
    assert "http" not in texto and "previa" not in texto.lower() and "PREVIA" not in texto


def test_o_json_e_legivel_e_tem_a_conversa(conversa, politica):
    texto = _entrada(conversa, politica).como_json()
    assert "quanto eu devo?" in texto  # ensure_ascii=False
    assert "\n  " in texto  # indent=2
    assert json.loads(texto)["conversa"] == [
        ["ABERTURA", "[abertura]"],
        ["CLIENTE", "quanto eu devo?"],
    ]


def test_o_basico_da_entrada(conversa, politica):
    entrada = _entrada(conversa, politica, usadas=2)
    assert isinstance(entrada, EntradaTurno)
    assert entrada.nome == "Cliente"
    assert entrada.regua == "R3" and entrada.tem_desconto is True
    assert (entrada.mensagens_usadas, entrada.teto) == (2, politica.teto_resolucao)
    assert entrada.novas == ("e o desconto?",)
    assert entrada.conversa == tuple(HISTORICO)


def test_varias_mensalidades_com_tres_e_nao_com_uma(conversa, politica):
    assert conversa.cobranca.mensalidades_abertas == 3
    assert _entrada(conversa, politica).varias_mensalidades is True
    conversa.cobranca.mensalidades_abertas = 1
    assert _entrada(conversa, politica).varias_mensalidades is False


def test_tem_desconto_e_falso_na_regua_sem_desconto(conversa, politica):
    conversa.cobranca.regua = Regua.R1
    entrada = _entrada(conversa, politica)
    assert entrada.tem_desconto is False
    assert entrada.oferta["ha_degrau_seguinte"] is False


def test_a_oferta_traz_o_degrau_o_degrau_seguinte_e_a_forma(conversa, politica):
    assert _entrada(conversa, politica).oferta == {
        "degrau_atual": 0,
        "ha_degrau_seguinte": True,
        "forma_enviada": None,
    }
    conversa.degrau_atual = 3
    conversa.forma_enviada = FormaPagamento.LINK_CTN
    assert _entrada(conversa, politica).oferta == {
        "degrau_atual": 3,
        "ha_degrau_seguinte": False,
        "forma_enviada": FormaPagamento.LINK_CTN.value,
    }


@pytest.mark.parametrize("propenso", [False, True])
def test_cada_acao_traz_os_marcadores_da_previa(conversa, politica, propenso):
    conversa.cobranca.propenso_cartao = propenso
    entrada = _entrada(conversa, politica)

    assert tuple(a["acao"] for a in entrada.acoes) == tuple(a.value for a in entrada.permitidas)
    assert entrada.permitidas == TODAS
    for item in entrada.acoes:
        acao = Acao(item["acao"])
        previa = previa_numeros(
            [acao, *anexar(acao, conversa.cobranca, tem_meio_vigente=False)],
            conversa,
            conversa.cobranca,
            politica,
            AGORA,
        )
        obrigatorios, opcionais = marcadores_do_turno(previa)
        assert item["marcadores_obrigatorios"] == obrigatorios
        assert item["marcadores_opcionais"] == opcionais
        assert item["quando"] and item["faz"]
        assert "sensível" not in item["quando"]


def _item(entrada, acao):
    [item] = [a for a in entrada.acoes if a["acao"] == acao]
    return item


@pytest.mark.parametrize("propenso", [False, True])
def test_a_answer_na_entrada_traz_quando_faz_e_os_marcadores(conversa, politica, propenso):
    conversa.cobranca.propenso_cartao = propenso
    conversa.degrau_atual = 1
    item = _item(_entrada(conversa, politica), "ANSWER")

    assert item["quando"] and item["faz"]
    assert item["faz"] != _FAZ_NONE
    assert item["marcadores_obrigatorios"] == ["link_ctn" if propenso else "pix_copia_e_cola"]
    assert {"divida", "mensalidades", "valor_final", "desconto"} <= set(
        item["marcadores_opcionais"]
    )


def test_a_answer_no_degrau_zero_nao_traz_valor_final_nem_desconto(conversa, politica):
    item = _item(_entrada(conversa, politica), "ANSWER")

    assert "valor_final" not in item["marcadores_opcionais"]
    assert "desconto" not in item["marcadores_opcionais"]
    assert "divida" in item["marcadores_opcionais"]


def test_com_meio_vigente_a_answer_nao_exige_marcador(conversa, politica):
    _com_pix_vigente(conversa)

    assert _item(_entrada(conversa, politica), "ANSWER")["marcadores_obrigatorios"] == []


def test_o_esquema_da_saida_aceita_a_answer(conversa, politica):
    permitidas = acoes_permitidas(conversa, conversa.cobranca, politica)

    assert "ANSWER" in esquema_da_saida(permitidas)["properties"]["acao"]["enum"]


def test_o_inform_debt_leva_o_link_so_de_quem_e_propenso(conversa, politica):
    def obrigatorios(entrada):
        [item] = [a for a in entrada.acoes if a["acao"] == "INFORM_DEBT"]
        return item["marcadores_obrigatorios"]

    assert obrigatorios(_entrada(conversa, politica)) == ["divida", "pix_copia_e_cola"]
    conversa.cobranca.propenso_cartao = True
    assert obrigatorios(_entrada(conversa, politica)) == ["divida", "link_ctn"]


def _com_pix_vigente(conversa):
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


@pytest.mark.parametrize("propenso", [False, True])
def test_a_entrada_traz_paga_pix_todo_mes_no_json(conversa, politica, propenso):
    conversa.cobranca.propenso_cartao = propenso

    entrada = _entrada(conversa, politica)

    assert entrada.paga_pix_todo_mes is propenso
    assert json.loads(entrada.como_json())["paga_pix_todo_mes"] is propenso


def test_a_entrada_traz_o_meio_vigente_e_no_json(conversa, politica):
    assert _entrada(conversa, politica).meio_vigente is None
    assert json.loads(_entrada(conversa, politica).como_json())["meio_vigente"] is None

    _com_pix_vigente(conversa)

    entrada = _entrada(conversa, politica)
    assert entrada.meio_vigente == "PIX"
    assert json.loads(entrada.como_json())["meio_vigente"] == "PIX"


@pytest.mark.parametrize("propenso", [False, True])
def test_com_meio_vigente_o_inform_debt_so_exige_a_divida(conversa, politica, propenso):
    conversa.cobranca.propenso_cartao = propenso
    _com_pix_vigente(conversa)

    [item] = [a for a in _entrada(conversa, politica).acoes if a["acao"] == "INFORM_DEBT"]

    assert item["marcadores_obrigatorios"] == ["divida"]


def test_com_meio_vigente_o_desconto_continua_com_o_link(conversa, politica):
    _com_pix_vigente(conversa)

    [item] = [a for a in _entrada(conversa, politica).acoes if a["acao"] == "OFFER_DISCOUNT"]

    assert "link_ctn" in item["marcadores_obrigatorios"]


def test_as_chaves_do_glossario_sao_as_dos_marcadores(conversa, politica):
    entrada = _entrada(conversa, politica)
    assert tuple(GLOSSARIO_MARCADORES) == MARCADORES
    assert entrada.glossario == GLOSSARIO_MARCADORES
    assert "R$" not in json.dumps(GLOSSARIO_MARCADORES, ensure_ascii=False)


def test_a_politica_traz_as_nove_regras_e_o_recebedor_so_se_preenchido(conversa, politica):
    assert len(POLITICA_COBRANCA) == 9
    assert _entrada(conversa, politica).politica == POLITICA_COBRANCA

    com_recebedor = politica.model_copy(update={"pix_recebedor": "Recebedor Fictício"})
    regras = _entrada(conversa, com_recebedor).politica
    assert regras[:9] == POLITICA_COBRANCA and len(regras) == 10
    assert "recebedor" in regras[9].lower()
    assert "Recebedor Fictício" not in regras[9]


def test_a_politica_traz_as_regras_da_secao_8_da_spec():
    texto = " ".join(POLITICA_COBRANCA).lower()
    # Promessa de pagamento: o cliente paga quando puder, pedindo o meio de novo; nada é registrado.
    assert "quando puder" in texto and "pedindo o pix ou o link de novo" in texto
    assert "não registra promessa" in texto and "prazo" in texto
    # Boleto e conta de energia vão ao atendente pelos sinais (sem_resposta, quer_conta_energia).
    assert "boleto" not in texto and "energia" not in texto
    assert "paga pix todo mês" in texto and "manualmente" in texto
    assert "corte" in texto and "cancelamento" in texto and "atraso" in texto


def test_a_politica_nao_traz_digito_valor_nem_link():
    texto = " ".join(POLITICA_COBRANCA)
    assert not any(letra.isdigit() for letra in texto)
    assert "R$" not in texto and "http" not in texto


def test_as_regras_da_politica_dizem_o_que_a_spec_manda():
    texto = " ".join(POLITICA_COBRANCA).lower()
    assert "parcelamento" in texto and "à vista" in texto
    assert "só vale" in texto and "link" in texto
    assert "pix" in texto and "integral" in texto
    assert "cadastrar o cartão" in texto
    assert "clínicas" in texto and "coberturas" in texto
    assert "valor final" in texto and "outro desconto" in texto


def test_as_novas_ficam_separadas_da_conversa(conversa, politica):
    dados = json.loads(_entrada(conversa, politica, novas=("oi", "tudo bem?")).como_json())
    assert dados["novas"] == ["oi", "tudo bem?"]
    assert ["CLIENTE", "oi"] not in dados["conversa"]


# o prompt


@pytest.mark.parametrize("campo", [*SINAIS, "tom", "acao", "reply", *TONS_UMA_CHAMADA])
def test_cada_campo_da_saida_aparece_no_sistema(campo):
    assert campo in SISTEMA


def test_o_sistema_traz_as_regras_de_redacao_e_as_proibicoes():
    texto = " ".join(SISTEMA.split())
    assert "marcador" in texto and "R$" in texto
    assert "terceiro" in texto
    assert "[áudio transcrito:" in texto
    assert "Não existe parcelamento" in texto
    assert "varias_mensalidades" in texto
    assert "{mensalidades}" in texto


def test_o_sistema_diz_o_objetivo_e_onde_o_reply_vale():
    texto = " ".join(SISTEMA.split())
    assert "O objetivo de toda mensagem é o pagamento, no menor número de mensagens" in texto
    assert "ações com texto e na passagem ao atendente" in texto
    assert "no NONE e no terceiro, fica vazio" in texto
    assert "Termine no próximo passo para pagar" in texto and "meio_vigente" in texto


def test_o_sistema_manda_escrever_a_passagem_ao_atendente():
    texto = " ".join(SISTEMA.split())
    passagem = texto.split("Passagem ao atendente")[1]
    for sinal in ("sinal de saída", "quer_humano", "sem_resposta", "irritado ou hostil"):
        assert sinal in passagem.split("Se perguntarem")[0]
    assert "uma ou duas frases" in passagem and "um atendente da unidade" in passagem
    assert "Sem marcador, sem valor, sem promessa" in passagem
    assert "sem opinar se ele tem razão" in passagem


def test_o_sistema_traz_as_regras_do_estilo_da_resposta():
    texto = " ".join(SISTEMA.split())
    assert "Dúvida junto de um pedido: a ação é a do pedido, e o texto responde à dúvida" in texto
    assert "acolha em uma frase e use OFFER_DISCOUNT, se estiver na lista; senão, ANSWER" in texto
    assert "assistente virtual do Cartão de Todos" in texto
    assert "não repita frase que já está no histórico" in texto


def test_o_sistema_explica_paga_pix_todo_mes_e_meio_vigente():
    texto = " ".join(SISTEMA.split())
    assert "paga_pix_todo_mes (o cliente costuma pagar por Pix todo mês" in texto
    assert "meio_vigente (o Pix ou o link que ele ainda pode usar, ou null)" in texto


def test_o_sistema_nao_diz_mais_que_o_texto_so_vale_nas_acoes_de_cobranca():
    texto = " ".join(SISTEMA.split())
    assert "só nas ações de cobrança" not in texto
    assert "nas outras, ele fica vazio" not in texto
    assert "leve a preocupação a sério" not in texto  # a linha do golpe: ele leva a frase fixa


def test_o_none_e_so_cortesia_e_na_duvida_e_answer():
    quando = CRITERIOS_DA_ACAO[Acao.NONE]
    assert "só confirma que leu ou agradece" in quando and "Na dúvida, ANSWER" in quando
    assert "pergunta" not in quando.replace("sem pergunta", "")


def test_os_criterios_das_acoes_dizem_a_regra_da_spec():
    assert "primeira apresentação da dívida" in CRITERIOS_DA_ACAO[Acao.INFORM_DEBT]
    # Depois da 1ª apresentação, a pergunta sobre valor é ANSWER: a regra mora só no critério dela.
    assert "depois de a dívida já ter sido informada" in CRITERIOS_DA_ACAO[Acao.ANSWER]
    assert "desempregado" in CRITERIOS_DA_ACAO[Acao.OFFER_DISCOUNT]
    assert "não quer ou não pode usar o cartão" in CRITERIOS_DA_ACAO[Acao.GENERATE_PIX]


def test_o_texto_do_pix_e_livre_so_as_3_linhas_de_dados_seguem_o_formato():
    faz = DESCRICAO_ACOES["GENERATE_PIX"]
    assert "texto é livre" in faz and "3 linhas de dados" in faz
    assert "CNPJ" in faz and "nada depois" in faz
    assert repr(TEXTO_DO_PIX) in faz
    assert "{pix_copia_e_cola}" in GLOSSARIO_MARCADORES["pix_copia_e_cola"]
    assert "CNPJ" in GLOSSARIO_MARCADORES["pix_copia_e_cola"]


def test_o_fora_do_assunto_com_texto_diz_so_que_o_canal_e_da_mensalidade():
    faz = DESCRICAO_ACOES["OUT_OF_SCOPE"]
    assert "frase pronta" not in faz and "reply fica vazio" not in faz
    assert "mensalidade" in faz and "não afirme nada" in faz.lower()


def test_o_sistema_diz_que_desabafo_sem_aspereza_nao_e_irritado():
    texto = " ".join(SISTEMA.split())
    assert "Desabafo sem aspereza não é irritado" in texto
    assert "Só raiva, aspereza, xingamento ou ameaça" in texto


def test_o_sistema_nao_deixa_repetir_o_valor_que_o_cliente_citou():
    # A21 (2026-11-05): "75 reais", "60,00" e "sessenta reais" reprovavam o texto.
    texto = " ".join(SISTEMA.split("Como escrever o reply")[1].split())
    # Ensaio de 2026-11-17: "só uma mensalidade" também reprovava; a quantidade entrou na regra.
    regra = (
        'Não repita valor nem quantidade que o cliente citou ("uma mensalidade"), em dígito nem '
        'por extenso; diga "o valor que você citou" ou "só uma parte".'
    )
    assert regra in texto


def test_montar_mensagens_devolve_o_sistema_e_o_usuario(conversa, politica):
    entrada = _entrada(conversa, politica)

    sistema, usuario = montar_mensagens(entrada)

    assert sistema == SISTEMA
    assert usuario == entrada.como_json()


def test_com_reprovadas_antes_o_usuario_traz_os_motivos(conversa, politica):
    entrada = _entrada(conversa, politica)

    _, usuario = montar_mensagens(entrada, ["valor_em_digitos", "marcador_faltando:divida"])

    assert usuario.startswith(entrada.como_json())
    assert "valor_em_digitos" in usuario and "marcador_faltando:divida" in usuario
    assert "reprovada" in usuario


def test_com_meio_vigente_o_sistema_manda_lembrar_sem_o_marcador():
    # Ensaio de 2026-11-17: com o Pix já enviado, o Luna escrevia {pix_copia_e_cola} sem valor.
    texto = " ".join(SISTEMA.split())
    assert "sem escrever o marcador dele" in texto


def test_cumprimento_e_comentario_nunca_sao_sem_resposta():
    # Ensaio de 2026-11-17: "Oi" e "Ok, vou ver aqui" iam ao atendente como sem_resposta.
    assert "cumprimento, confirmação e comentário nunca" in SINAIS_DO_PROMPT["sem_resposta"]
