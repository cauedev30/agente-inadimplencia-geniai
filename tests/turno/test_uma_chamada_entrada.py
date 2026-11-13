"""A entrada do turno de uma chamada: a política, as ações permitidas e a prévia (D47, E1.2)."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.pix import PixFalso
from agente.politica import Regua
from agente.tipos import FormaPagamento
from agente.turno.acoes import Acao, anexar
from agente.turno.executar import executar
from agente.turno.redacao import MARCADORES, marcadores_do_turno
from agente.turno.sinais import GATILHOS
from agente.turno.uma_chamada import (
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
    assert SINAIS == (*SINAIS_DE_SAIDA, "quer_humano", "terceiro")
    assert len(SINAIS) == 10


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


def test_o_humano_nunca_e_uma_acao_permitida(conversa, politica):
    for regua in Regua:
        conversa.cobranca.regua = regua
        assert Acao.HUMAN not in acoes_permitidas(conversa, conversa.cobranca, politica)


# previa_numeros


@pytest.mark.parametrize("propenso", [False, True])
@pytest.mark.parametrize("acao", TODAS)
def test_a_previa_bate_com_o_executar(conversa, politica, acao, propenso):
    conversa.cobranca.propenso_cartao = propenso
    acoes = [acao, *anexar(acao, conversa.cobranca)]

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
    assert (pix.br_code, pix.pix_vence_em) == ("PREVIA", AGORA)
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
            [acao, *anexar(acao, conversa.cobranca)], conversa, conversa.cobranca, politica, AGORA
        )
        obrigatorios, opcionais = marcadores_do_turno(previa)
        assert item["marcadores_obrigatorios"] == obrigatorios
        assert item["marcadores_opcionais"] == opcionais
        assert item["quando"] and item["faz"]
        assert "sensível" not in item["quando"]


def test_o_inform_debt_leva_o_link_so_de_quem_e_propenso(conversa, politica):
    def obrigatorios(entrada):
        [item] = [a for a in entrada.acoes if a["acao"] == "INFORM_DEBT"]
        return item["marcadores_obrigatorios"]

    assert obrigatorios(_entrada(conversa, politica)) == ["divida", "pix_copia_e_cola"]
    conversa.cobranca.propenso_cartao = True
    assert obrigatorios(_entrada(conversa, politica)) == ["divida", "link_ctn"]


def test_as_chaves_do_glossario_sao_as_dos_marcadores(conversa, politica):
    entrada = _entrada(conversa, politica)
    assert tuple(GLOSSARIO_MARCADORES) == MARCADORES
    assert entrada.glossario == GLOSSARIO_MARCADORES
    assert "R$" not in json.dumps(GLOSSARIO_MARCADORES, ensure_ascii=False)


def test_a_politica_traz_as_seis_regras_e_o_recebedor_so_se_preenchido(conversa, politica):
    assert len(POLITICA_COBRANCA) == 6
    assert _entrada(conversa, politica).politica == POLITICA_COBRANCA

    com_recebedor = politica.model_copy(update={"pix_recebedor": "Recebedor Fictício"})
    regras = _entrada(conversa, com_recebedor).politica
    assert regras[:6] == POLITICA_COBRANCA and len(regras) == 7
    assert "recebedor" in regras[6].lower()
    assert "Recebedor Fictício" not in regras[6]


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


def test_o_sistema_diz_que_desabafo_sem_aspereza_nao_e_irritado():
    texto = " ".join(SISTEMA.split())
    assert "Desabafo sem aspereza não é irritado" in texto
    assert "Só raiva, aspereza, xingamento ou ameaça" in texto


def test_o_sistema_nao_deixa_repetir_o_valor_que_o_cliente_citou():
    # A21 (2026-11-05): "75 reais", "60,00" e "sessenta reais" reprovavam o texto.
    texto = " ".join(SISTEMA.split("Como escrever o reply")[1].split())
    regra = (
        'Não repita valor que o cliente citou, em dígito nem por extenso; diga "o valor que '
        'você citou".'
    )
    assert regra in texto


def test_montar_mensagens_devolve_o_sistema_e_o_usuario(conversa, politica):
    entrada = _entrada(conversa, politica)

    sistema, usuario = montar_mensagens(entrada)

    assert sistema == SISTEMA
    assert usuario == entrada.como_json()


def test_com_reprovadas_antes_o_usuario_traz_os_motivos(conversa, politica):
    entrada = _entrada(conversa, politica)

    _, usuario = montar_mensagens(entrada, ["valor_em_digitos", "acao_nao_permitida:HUMAN"])

    assert usuario.startswith(entrada.como_json())
    assert "valor_em_digitos" in usuario and "acao_nao_permitida:HUMAN" in usuario
    assert "reprovada" in usuario
