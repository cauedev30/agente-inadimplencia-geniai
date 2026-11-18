"""Os marcadores do LLM e a interpolação pelo backend (tarefa 5.2): número errado não entra."""

from zoneinfo import ZoneInfo

import pytest

from agente.turno.acoes import Acao
from agente.turno.catalogo import TEXTO_DO_PIX
from agente.turno.executar import Numeros
from agente.turno.redacao import (
    MARCADORES,
    RedacaoInvalida,
    anexar_recebedor,
    interpolar,
    marcadores_do_turno,
    motivos_do_texto,
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


def test_o_pix_sai_com_o_valor_e_a_chave():
    numeros = _numeros(Acao.GENERATE_PIX)
    texto = "O Pix é de {valor_pix}.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
    assert interpolar(texto, numeros) == (
        "O Pix é de R$ 100,20.\nPara pagar, use o Pix CNPJ: 00020126FALSOabc10020"
    )


@pytest.mark.parametrize(
    "texto",
    [
        # O que o Luna escreveu no ensaio de 2026-11-17: a frase do Pix na mesma linha da dívida,
        # a chave na linha de baixo do "CNPJ:" e o ponto grudado na chave.
        "O Pix é de {valor_pix}. Para pagar, use o Pix CNPJ: {pix_copia_e_cola}",
        "O Pix é de {valor_pix}. Para pagar, use o Pix CNPJ:\n{pix_copia_e_cola}",
        "O Pix é de {valor_pix}.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}.",
    ],
)
def test_o_codigo_poe_a_frase_do_pix_na_linha_de_baixo_e_a_chave_no_fim(texto):
    assert interpolar(texto, _numeros(Acao.GENERATE_PIX)) == (
        "O Pix é de R$ 100,20.\nPara pagar, use o Pix CNPJ: 00020126FALSOabc10020"
    )


@pytest.mark.parametrize(
    "texto",
    [
        # Seco, sem dizer que é CNPJ.
        "O Pix é de {valor_pix}.\nPara pagar, use o Pix: {pix_copia_e_cola}",
        # Texto depois da chave, na mesma linha.
        "O Pix é de {valor_pix}.\nUse o Pix CNPJ: {pix_copia_e_cola} e me mande o comprovante.",
        # O que o Luna escreveu no ensaio de 2026-11-17: a frase que anuncia a chave sai junto.
        "O Pix é de {valor_pix}. Para pagar pelo Pix, use esta chave:\n{pix_copia_e_cola}",
        # Esqueceu a chave.
        "O Pix é de {valor_pix}.",
    ],
)
def test_a_chave_fora_do_formato_ou_esquecida_o_codigo_escreve_a_linha_dela(texto):
    assert interpolar(texto, _numeros(Acao.GENERATE_PIX)) == (
        "O Pix é de R$ 100,20.\nPara pagar, use o Pix CNPJ: 00020126FALSOabc10020"
    )


def test_a_chave_sem_outro_texto_e_reprovada():
    # Tudo numa frase só: tirada a linha da chave, não sobra texto do Luna.
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar(
            "O total é {valor_pix} e para pagar use o Pix: {pix_copia_e_cola}",
            _numeros(Acao.GENERATE_PIX),
        )
    assert erro.value.motivos == ["pix_fora_da_linha_do_cnpj"]


def test_o_texto_fixo_do_botao_pix_passa_na_regra_da_linha():
    assert "00020126FALSOabc10020\n" in interpolar(TEXTO_DO_PIX, _numeros(Acao.GENERATE_PIX))


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
        "Ligue para a central ① ②: {divida}",  # dígito dentro do círculo
        "Fica por R² {divida}",  # o expoente vale um número
        "Fica ½ do valor: {divida}",  # a fração vale um número
    ],
)
def test_qualquer_valor_escrito_pelo_llm_e_barrado(texto):
    with pytest.raises(RedacaoInvalida, match="valor_em_digitos"):
        interpolar(texto, _numeros(Acao.INFORM_DEBT))


@pytest.mark.parametrize(
    "texto",
    [
        "Fica setenta reais: {divida}",
        "Fica setenta Reais: {divida}",
        "Fica R$ {divida}, com dez por cento a menos",
        "Tem 10% a menos: {divida}",
        "Paga em trinta dias: {divida}",
        "Paga em três dias: {divida}",  # o acento não escapa
        "São duzentos e cinquenta: {divida}",
        "Pague em um dia: {divida}",  # o um que indica quantidade
        "Fica por mil e quinhentos: {divida}",
        "Sobram cinquenta centavos: {divida}",
        "Fica dois mil: {divida}",
        "Fica mil reais: {divida}",
        "Pague em duas parcelas: {divida}",
        "São quinhentos reais: {divida}",
        "Hoje sai por noventa: {divida}",  # o numeral de dezena ou centena vale solto
        "Fica por cem: {divida}",
        "Fica por cento e dez: {divida}",
        "Fica por dez: {divida}",
        "Fica por novecentas: {divida}",
        "Pague a metade: {divida}",
    ],
)
def test_valor_e_prazo_por_extenso_sao_barrados(texto):
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar(texto, _numeros(Acao.INFORM_DEBT))
    assert "valor_por_extenso" in erro.value.motivos


@pytest.mark.parametrize(
    "texto",
    [
        "Pague em www.exemplo.com/pagar. {divida}",
        "Pague em exemplo.com.br. {divida}",
        "Pague em HTTP://exemplo/pagar {divida}",
        "Pague em https://pagamento-falso.example/x {divida}",
        "Pague em pagamento.net {divida}",
        "Veja em cartaodetodos.shop. {divida}",  # o tld fora da lista também é domínio
        "Pague em pague.site {divida}",
        "Veja loja.pagamentos/x {divida}",
    ],
)
def test_link_ou_dominio_inventado_e_barrado(texto):
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar(texto, _numeros(Acao.INFORM_DEBT))
    assert "link_nao_autorizado" in erro.value.motivos


@pytest.mark.parametrize(
    "texto",
    [
        "Pague em PAGAMENTO.COM {divida}",
        "Pague em pagamento.Com/pagar {divida}",
        "Pague em pagamento.Com. {divida}",
        "Pague em pagamento.Com",
        "Veja www.Exemplo.Com {divida}",
        "Veja portal.Online agora {divida}",
        "Entre em atendimento.Site hoje {divida}",
    ],
)
def test_link_em_maiuscula_continua_barrado(texto):
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar(texto, _numeros(Acao.INFORM_DEBT))
    assert "link_nao_autorizado" in erro.value.motivos


@pytest.mark.parametrize(
    "texto",
    [
        "Combinado, vou verificar agora.Com isso fica resolvido. {divida}",
        "Estou à disposição.Me avise se tiver dúvida. {divida}",
        "Obrigado.Até logo. {divida}",
        "Combinado.Vamos resolver isso. {divida}",
        "Pode pagar agora.Com agora mesmo fica melhor. {divida}",
    ],
)
def test_ponto_sem_espaco_entre_frases_nao_e_link(texto):
    assert interpolar(texto, _numeros(Acao.INFORM_DEBT))


def test_os_motivos_novos_se_somam_aos_antigos():
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("Fica setenta reais em www.x.com", _numeros(Acao.INFORM_DEBT))
    assert erro.value.motivos == [
        "valor_por_extenso",
        "link_nao_autorizado",
        "marcador_faltando:divida",
    ]


@pytest.mark.parametrize(
    "texto",
    [
        "Um abraço!",
        "Oi, Maria! Tenha uma ótima tarde.",
        "Tenho um desconto para você.",
        "Bom dia! Uma boa notícia para você.",
        "Entendo o seu incômodo, e obrigado pelo retorno.",
        "Tudo bem. Vamos resolver isso por aqui.",
        "Mil desculpas pelo transtorno.",
        "Posso ajudar com os dois assuntos.",
        "Entendo que o problema é real.",
        "Zero burocracia para resolver.",
        "Os dois assuntos ficam resolvidos.",
        "Vou olhar as três opções.",
        "Fico no aguardo, são só nove perguntas.",
    ],
)
def test_o_texto_normal_passa(texto):
    numeros = _numeros(Acao.INFORM_DEBT, Acao.OFFER_DISCOUNT, Acao.SEND_CTN_LINK, degrau=1)
    assert interpolar(f"{texto} {{divida}} {{valor_final}} {{link_ctn}}", numeros)


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


def test_o_valor_com_desconto_so_existe_com_degrau():
    numeros = _numeros(Acao.SEND_CTN_LINK)
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("São {valor_final} pelo link {link_ctn}", numeros)
    assert erro.value.motivos == ["marcador_sem_valor:valor_final"]


def _com_recebedor(politica, nome="Cartão de Todos Ltda"):
    return politica.model_copy(update={"pix_recebedor": nome})


def test_anexar_recebedor_poe_a_frase_uma_vez_no_fim(politica):
    numeros = _numeros(Acao.GENERATE_PIX)
    texto = anexar_recebedor("Segue o Pix.", numeros, _com_recebedor(politica))
    assert texto == (
        "Segue o Pix.\n\nAo colar o código no seu banco, confira que o recebedor é "
        "Cartão de Todos Ltda."
    )
    assert texto.count("Cartão de Todos Ltda") == 1


def test_anexar_recebedor_com_o_nome_vazio_nao_muda_o_texto(politica):
    numeros = _numeros(Acao.GENERATE_PIX)
    assert politica.pix_recebedor == ""
    assert anexar_recebedor("Segue o Pix.", numeros, politica) == "Segue o Pix."


@pytest.mark.parametrize(
    "acoes", [(Acao.SEND_CTN_LINK,), (Acao.INFORM_DEBT,), (Acao.OUT_OF_SCOPE,)]
)
def test_anexar_recebedor_sem_pix_no_turno_nao_muda_o_texto(acoes, politica):
    numeros = _numeros(*acoes)
    assert anexar_recebedor("Segue o link.", numeros, _com_recebedor(politica)) == "Segue o link."


def test_anexar_recebedor_nao_interpreta_chaves_nem_digitos_do_nome(politica):
    # O nome vai como está: CNPJ com dígitos e chaves não quebram nem passam pelo `interpolar`.
    numeros = _numeros(Acao.GENERATE_PIX)
    nome = "CDT {Franquia} 12.345.678/0001-90"
    texto = anexar_recebedor("Oi.", numeros, _com_recebedor(politica, nome))
    assert texto.endswith(f"o recebedor é {nome}.")


@pytest.mark.parametrize(
    "texto, esperados",
    [
        ("Bom dia, tudo bem?", []),
        ("Fica R$ 70 pelo link", ["valor_em_digitos"]),
        ("Fica setenta reais", ["valor_por_extenso"]),
        ("Veja www.x.com", ["link_nao_autorizado"]),
        ("Ficou {xpto", ["marcador_malformado"]),
        (
            "R$ 70, setenta reais, www.x.com e }",
            ["valor_em_digitos", "valor_por_extenso", "link_nao_autorizado", "marcador_malformado"],
        ),
        ("Seu valor é {divida}, pelo {link_ctn}", []),
    ],
)
def test_motivos_do_texto_sao_os_do_interpolar_fora_dos_marcadores(texto, esperados):
    assert motivos_do_texto(texto) == esperados
    numeros = _numeros(Acao.INFORM_DEBT)
    try:
        interpolar(texto, numeros)
    except RedacaoInvalida as erro:
        # O interpolar soma os motivos dos marcadores depois destes quatro.
        assert erro.motivos[: len(esperados)] == esperados


# Os marcadores da `ANSWER` (plano de autonomia, tarefa 1.3): a resposta e o fora do assunto citam o
# valor com desconto do degrau já liberado, sem o link.


def test_a_answer_no_degrau_zero_so_tem_a_divida_e_as_mensalidades():
    numeros = _numeros(Acao.ANSWER)

    obrigatorios, opcionais = marcadores_do_turno(numeros)

    assert (obrigatorios, opcionais) == ([], ["divida", "mensalidades"])
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("Com o desconto fica {valor_final}.", numeros)
    assert erro.value.motivos == ["marcador_sem_valor:valor_final"]


def test_a_answer_no_degrau_1_aceita_o_valor_final_e_o_desconto():
    numeros = _numeros(Acao.ANSWER, degrau=1)

    texto = interpolar("São {divida}; com {desconto} de desconto, {valor_final}.", numeros)

    assert texto == "São R$ 100,20; com R$ 30,00 de desconto, R$ 70,20."
    assert marcadores_do_turno(numeros)[1] == [
        "divida",
        "mensalidades",
        "valor_final",
        "desconto",
    ]


def test_o_out_of_scope_no_degrau_1_tambem_tem_o_valor_final():
    numeros = _numeros(Acao.OUT_OF_SCOPE, degrau=1)

    assert interpolar("Fica {valor_final}.", numeros) == "Fica R$ 70,20."


def test_a_answer_sem_anexo_com_o_link_e_reprovada():
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("Pague aqui: {link_ctn}", _numeros(Acao.ANSWER, degrau=1))
    assert erro.value.motivos == ["marcador_sem_valor:link_ctn"]


def test_a_answer_com_anexo_pix_leva_a_chave_mesmo_se_o_luna_esquecer():
    numeros = _numeros(Acao.ANSWER, Acao.GENERATE_PIX)

    assert marcadores_do_turno(numeros)[0] == ["pix_copia_e_cola"]
    assert interpolar("O valor é {divida}.", numeros) == (
        "O valor é R$ 100,20.\nPara pagar, use o Pix CNPJ: 00020126FALSOabc10020"
    )
    texto = "O valor é {divida}.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
    assert interpolar(texto, numeros).endswith("00020126FALSOabc10020")


def test_o_pix_sozinho_no_degrau_1_continua_sem_valor_final():
    numeros = _numeros(Acao.GENERATE_PIX, degrau=1)

    _, opcionais = marcadores_do_turno(numeros)
    assert "valor_final" not in opcionais and "desconto" not in opcionais
    with pytest.raises(RedacaoInvalida) as erro:
        interpolar("Fica {valor_final}.\nPix CNPJ: {pix_copia_e_cola}", numeros)
    assert erro.value.motivos == ["marcador_sem_valor:valor_final"]
