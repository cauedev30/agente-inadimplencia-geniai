"""Os marcadores do LLM e a interpolação pelo backend (tarefa 5.2): número errado não entra."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agente.integracoes.redator import RedatorFalso
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo
from agente.turno.acoes import Acao
from agente.turno.executar import Numeros
from agente.turno.jev1 import PERGUNTAS_JEV1, LeituraJev1
from agente.turno.redacao import (
    MARCADORES,
    RedacaoInvalida,
    interpolar,
    marcadores_do_turno,
    montar_pedido,
)

JP = ZoneInfo("America/Fortaleza")


def _numeros(*acoes, degrau=0, divida=10020):
    acoes = tuple(acoes)
    valor_final = divida - 3000 * degrau
    campos = {}
    if Acao.SEND_CTN_LINK in acoes:
        campos["link_ctn"] = "https://ctn.invalid/pagar/pagar-1"
    if Acao.GENERATE_PIX in acoes:
        campos |= {
            "valor_pix_centavos": divida,
            "br_code": "00020126FALSOabc10020",
            "pix_vence_em": datetime(2026, 10, 27, 10, 5, tzinfo=JP),
        }
    return Numeros(
        acoes=acoes,
        divida_centavos=divida,
        mensalidades=3,
        degrau=degrau,
        desconto_centavos=divida - valor_final,
        valor_final_centavos=valor_final,
        **campos,
    )


def _leitura(**respostas):
    laudos = []
    for pergunta in PERGUNTAS_JEV1:
        valor = respostas.get(pergunta.nome, False if pergunta.tipo == "bool" else "neutro")
        if pergunta.nome == "intencao_pagar":
            valor = respostas.get(pergunta.nome, "nenhuma")
        laudos.append(Laudo(pergunta.nome, valor, 0.95, 0.9))
    return LeituraJev1(tuple(laudos))


def test_formata_o_real_em_pt_br():
    numeros = _numeros(Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK, degrau=1)
    texto = "Era {divida}, com {desconto} de desconto fica {valor_final}: {link_ctn}"

    assert interpolar(texto, numeros) == (
        "Era R$ 100,20, com R$ 30,00 de desconto fica R$ 70,20: https://ctn.invalid/pagar/pagar-1"
    )


def test_formata_o_milhar_e_as_mensalidades():
    numeros = _numeros(Acao.INFORM_DEBT, divida=100200)
    assert interpolar("São {mensalidades} mensalidades: {divida}.", numeros) == (
        "São 3 mensalidades: R$ 1.002,00."
    )


def test_o_pix_sai_com_o_copia_e_cola_e_o_vencimento():
    numeros = _numeros(Acao.GENERATE_PIX)
    texto = "Pix de {valor_pix}, vale até {pix_vencimento}: {pix_copia_e_cola}"
    assert interpolar(texto, numeros) == (
        "Pix de R$ 100,20, vale até 27/10 às 10:05: 00020126FALSOabc10020"
    )


def test_o_r_70_cru_escrito_pelo_llm_e_barrado():
    numeros = _numeros(Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK, degrau=1)
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("Fica R$ 70 pelo link {link_ctn}, de {valor_final}", numeros)
    assert erro.value.motivos == ["valor_em_digitos"]


@pytest.mark.parametrize(
    "texto",
    [
        "Pague em 24 horas: {divida}",  # dígito solto
        "O valor é R$ {divida}",  # o R$ escrito pelo LLM duplica o do backend
        "O valor é R$ setenta: {divida}",
        "O valor é ٣ reais: {divida}",  # dígito de outro alfabeto
    ],
)
def test_qualquer_valor_escrito_pelo_llm_e_barrado(texto):
    with pytest.raises(RedacaoInvalida, match="valor_em_digitos"):
        interpolar(texto, _numeros(Acao.INFORM_DEBT))


def test_falta_o_marcador_do_link_quando_ha_send_ctn_link():
    numeros = _numeros(Acao.INFORM_DEBT, Acao.SEND_CTN_LINK)
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("O valor é {divida}. Pague pelo link.", numeros)
    assert erro.value.motivos == ["marcador_faltando:link_ctn"]


def test_marcador_desconhecido_e_barrado():
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("O total é {valor_total}, ou {divida}.", _numeros(Acao.INFORM_DEBT))
    assert erro.value.motivos == ["marcador_desconhecido:valor_total"]


def test_marcador_de_acao_que_ficou_de_fora_e_barrado():
    # Sem SEND_CTN_LINK, não há link nem valor com desconto para pôr no texto.
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("O valor é {divida}, pague em {link_ctn}.", _numeros(Acao.INFORM_DEBT))
    assert erro.value.motivos == ["marcador_sem_valor:link_ctn"]


@pytest.mark.parametrize("texto", ["O valor é { divida }.", "O valor é {divida.", "{Divida}"])
def test_marcador_malformado_e_barrado(texto):
    with pytest.raises(RedacaoInvalida, match="marcador"):
        interpolar(texto, _numeros(Acao.INFORM_DEBT))


def test_os_motivos_se_somam():
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("R$ 70 em {xpto}", _numeros(Acao.INFORM_DEBT, Acao.SEND_CTN_LINK))
    assert erro.value.motivos == [
        "valor_em_digitos",
        "marcador_desconhecido:xpto",
        "marcador_faltando:divida",
        "marcador_faltando:link_ctn",
    ]


@pytest.mark.parametrize(
    ("acoes", "obrigatorios"),
    [
        ((Acao.INFORM_DEBT, Acao.GENERATE_PIX), ["divida", "pix_copia_e_cola"]),
        ((Acao.INFORM_DEBT, Acao.SEND_CTN_LINK), ["divida", "link_ctn"]),
        ((Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK), ["valor_final", "link_ctn"]),
        ((Acao.SEND_CTN_LINK,), ["link_ctn"]),
        ((Acao.GENERATE_PIX,), ["pix_copia_e_cola"]),
        ((Acao.OUT_OF_SCOPE,), []),
    ],
)
def test_os_marcadores_obrigatorios_de_cada_acao(acoes, obrigatorios):
    numeros = _numeros(*acoes, degrau=1 if Acao.OFFER_DISCOUNT in acoes else 0)
    obrigatorios_do_turno, opcionais = marcadores_do_turno(numeros)
    assert obrigatorios_do_turno == obrigatorios
    assert set(opcionais).isdisjoint(obrigatorios)
    assert set(opcionais) | set(obrigatorios) <= set(MARCADORES)


def test_o_desconto_so_existe_com_degrau():
    _, opcionais = marcadores_do_turno(_numeros(Acao.SEND_CTN_LINK))
    assert "desconto" not in opcionais
    _, opcionais = marcadores_do_turno(_numeros(Acao.SEND_CTN_LINK, degrau=2))
    assert "desconto" in opcionais


def test_o_pedido_leva_as_acoes_os_marcadores_e_o_tom(politica):
    numeros = _numeros(Acao.INFORM_DEBT, Acao.GENERATE_PIX)
    historico = [("ABERTURA", "[abertura]"), ("CLIENTE", "quanto eu devo?")]

    pedido = montar_pedido(
        numeros,
        _leitura(tom="impaciente"),
        Regua.R3,
        "Maria Fictícia da Silva",
        EstadoConversa.EM_RISCO,
        historico,
        politica,
    )

    assert pedido.acoes == ("INFORM_DEBT", "GENERATE_PIX")
    assert pedido.marcadores == ("divida", "pix_copia_e_cola")
    assert "valor_pix" in pedido.marcadores_opcionais
    assert pedido.nome_cliente == "Maria"
    assert pedido.estado == "EM_RISCO"
    assert pedido.historico == tuple(historico)
    assert "incômodo" in pedido.tom_alvo
    assert pedido.reprovadas_antes is None
    assert pedido.objecao_sem_desconto is False


def test_a_objecao_na_regua_sem_desconto_vai_no_pedido(politica):
    numeros = _numeros(Acao.INFORM_DEBT, Acao.GENERATE_PIX)
    leitura = _leitura(objecao_valor=True)
    pedido = montar_pedido(numeros, leitura, Regua.NR, "Ana", EstadoConversa.ENGAJADO, [], politica)
    assert pedido.objecao_sem_desconto is True
    pedido = montar_pedido(numeros, leitura, Regua.R3, "Ana", EstadoConversa.ENGAJADO, [], politica)
    assert pedido.objecao_sem_desconto is False


def test_o_pedido_da_reescrita_sabe_o_que_reprovou(politica):
    pedido = montar_pedido(
        _numeros(Acao.INFORM_DEBT),
        _leitura(),
        Regua.R3,
        "Ana",
        EstadoConversa.ENGAJADO,
        [],
        politica,
        reprovadas_antes=["tom_adequado"],
    )
    assert pedido.reprovadas_antes == ("tom_adequado",)


@pytest.mark.parametrize(
    "acoes",
    [
        (Acao.INFORM_DEBT, Acao.GENERATE_PIX),
        (Acao.INFORM_DEBT, Acao.SEND_CTN_LINK),
        (Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK),
        (Acao.SEND_CTN_LINK,),
        (Acao.GENERATE_PIX,),
        (Acao.OUT_OF_SCOPE,),
    ],
)
def test_o_texto_da_falsa_passa_na_interpolacao(acoes, politica):
    numeros = _numeros(*acoes, degrau=1 if Acao.OFFER_DISCOUNT in acoes else 0)
    pedido = montar_pedido(
        numeros, _leitura(), Regua.R3, "Ana", EstadoConversa.ENGAJADO, [], politica
    )
    texto = interpolar(RedatorFalso().redigir(pedido), numeros)
    assert "{" not in texto
