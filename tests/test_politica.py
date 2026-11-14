from datetime import date, time, timedelta

import pytest

from agente.politica import PoliticaInvalida, Regua, carregar_politica
from agente.tipos import FormaHistorico

from .conftest import RAIZ

ARQUIVO_PADRAO = RAIZ / "politica.toml"


def test_arquivo_padrao_carrega(politica):
    assert politica.mensalidade_centavos == 3340
    assert politica.teto_resolucao == 5
    assert politica.regua_acima_de_6 is Regua.R6


def test_o_link_baixa_sozinho_no_ctn_por_padrao(politica):
    assert politica.agente_da_baixa_no_link is False


def test_o_nome_antigo_da_baixa_no_link_e_recusado(tmp_path):
    texto = ARQUIVO_PADRAO.read_text(encoding="utf-8").replace(
        "agente_da_baixa_no_link", "ctn_baixa_no_link"
    )
    arquivo = tmp_path / "politica.toml"
    arquivo.write_text(texto, encoding="utf-8")
    with pytest.raises(PoliticaInvalida, match="agente_da_baixa_no_link"):
        carregar_politica(arquivo)


@pytest.mark.parametrize(
    ("regua", "desconto"),
    [
        (Regua.NR, 0),
        (Regua.R1, 0),
        (Regua.R2, 0),
        (Regua.R3, 9000),
        (Regua.PREVIA, 6000),
        (Regua.R4, 9000),
        (Regua.R5, 7500),
        (Regua.R6, 6000),
    ],
)
def test_desconto_maximo_por_regua(politica, regua, desconto):
    assert politica.reguas[regua].desconto_max_centavos == desconto
    assert politica.reguas[regua].n_degraus == 3


def test_janela_padrao(politica):
    janela = politica.janela
    assert janela.fuso == "America/Fortaleza"
    assert janela.dias["seg"].inicio == time(8, 0)
    assert janela.dias["sex"].fim == time(18, 0)
    assert janela.dias["sab"].fim == time(12, 0)
    assert "dom" not in janela.dias
    assert date(2026, 12, 25) in janela.feriados


def test_propensao(politica):
    assert politica.propensao.n_meses == 3
    assert politica.propensao.tolerancia_dias == 3


def _arquivo_com(tmp_path, original, novo):
    texto = ARQUIVO_PADRAO.read_text(encoding="utf-8")
    assert original in texto
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto.replace(original, novo, 1), encoding="utf-8")
    return caminho


@pytest.mark.parametrize(
    ("original", "novo", "campo"),
    [
        ("desconto_max_centavos = 9000", "desconto_max_centavos = -1", "desconto_max_centavos"),
        ("n_degraus = 3", "n_degraus = 0", "n_degraus"),
        ("teto_resolucao = 5", "teto_resolucao = 0", "teto_resolucao"),
        ('fuso = "America/Fortaleza"', 'fuso = "America/Lugar_Nenhum"', "fuso"),
        ('sab = ["08:00", "12:00"]', 'sab = ["12:00", "08:00"]', "sab"),
    ],
)
def test_valor_invalido_diz_o_campo(tmp_path, original, novo, campo):
    with pytest.raises(PoliticaInvalida, match=campo):
        carregar_politica(_arquivo_com(tmp_path, original, novo))


def test_regua_faltando_e_recusada(tmp_path):
    caminho = _arquivo_com(tmp_path, "[reguas.R5]", "[reguas.R5_FORA]")
    with pytest.raises(PoliticaInvalida, match="reguas"):
        carregar_politica(caminho)


def test_integracao_fora_tentativas_e_esperas_padrao(politica):
    assert politica.integracao_tentativas == 3
    assert politica.erro_interno_tentativas == 3
    assert [politica.espera_integracao(n) for n in (1, 2, 3, 4)] == [
        timedelta(seconds=60),
        timedelta(seconds=120),
        timedelta(seconds=240),
        timedelta(seconds=480),
    ]


def test_a_espera_da_integracao_nao_passa_do_teto(politica):
    assert politica.espera_integracao(50) == timedelta(seconds=3600)


def test_a_espera_maxima_menor_que_a_inicial_e_recusada(tmp_path):
    caminho = _arquivo_com(
        tmp_path,
        "integracao_espera_maxima_segundos = 3600",
        "integracao_espera_maxima_segundos = 30",
    )
    with pytest.raises(PoliticaInvalida, match="integracao_espera_maxima_segundos"):
        carregar_politica(caminho)


def test_telefones_da_equipe_sao_normalizados(tmp_path):
    caminho = _arquivo_com(
        tmp_path,
        "telefones_equipe = []",
        'telefones_equipe = ["(83) 90000-0001", "+5583900000002"]',
    )
    assert carregar_politica(caminho).telefones_equipe == {"+5583900000001", "+5583900000002"}


@pytest.mark.parametrize("telefone", ["123", "", "+1 202 555 0100"])
def test_telefone_da_equipe_invalido_e_recusado(tmp_path, telefone):
    caminho = _arquivo_com(tmp_path, "telefones_equipe = []", f'telefones_equipe = ["{telefone}"]')
    with pytest.raises(PoliticaInvalida, match="telefones_equipe"):
        carregar_politica(caminho)


def test_o_pix_a_menos_de_uma_hora_de_vencer_nao_e_reaproveitado_por_padrao(politica):
    assert politica.pix_reaproveitar_minimo_minutos == 60


def test_a_saida_no_whatsapp_tem_padrao(politica):
    assert politica.texto_humano_no_whatsapp == (
        "Entendo a sua preocupação. Um atendente da unidade vai continuar com você aqui mesmo, "
        "nesta conversa."
    )


def test_o_recebedor_do_pix_vem_desligado_e_o_texto_traz_o_marcador(politica):
    assert politica.pix_recebedor == ""
    assert politica.texto_pix_recebedor == (
        "Ao colar o código no seu banco, confira que o recebedor é {recebedor}."
    )


def test_o_recebedor_do_pix_preenchido_e_aceito(tmp_path):
    caminho = _arquivo_com(tmp_path, 'pix_recebedor = ""', 'pix_recebedor = "Cartão de Todos Ltda"')
    assert carregar_politica(caminho).pix_recebedor == "Cartão de Todos Ltda"


def test_o_texto_do_recebedor_sem_o_marcador_e_recusado(tmp_path):
    caminho = _arquivo_com(
        tmp_path,
        "confira que o recebedor é {recebedor}.",
        "confira o recebedor.",
    )
    with pytest.raises(PoliticaInvalida, match="texto_pix_recebedor"):
        carregar_politica(caminho)


def test_o_aviso_da_baixa_externa_tem_texto_curto_sem_valor_nem_nome(politica):
    texto = politica.texto_baixa_externa
    assert texto.strip()
    assert len(texto) <= 200
    assert "R$" not in texto
    assert "{" not in texto


def test_o_aviso_da_baixa_externa_nao_pode_ser_vazio(tmp_path):
    caminho = _arquivo_com(tmp_path, 'texto_baixa_externa = "', 'texto_baixa_externa = ""  # "')
    with pytest.raises(PoliticaInvalida, match="texto_baixa_externa"):
        carregar_politica(caminho)


# O modo teste (D56): o `[teste]` e o cardápio de casos inventados.
CARDAPIO = RAIZ / "cardapio-teste.toml"
PILOTO = 'liberacao = { modo = "piloto", telefones = ["+5583900000001", "+5583900000002"] }'
TESTE = """
[teste]
cardapio = "cardapio-teste.toml"
texto_abertura = "Oi, {nome}! Há {mensalidades} mensalidades em aberto, {valor}."
"""


def _politica_de_teste(tmp_path, *, liberacao=PILOTO, teste=TESTE, cardapio=None):
    texto = ARQUIVO_PADRAO.read_text(encoding="utf-8")
    texto = texto.replace('liberacao = { modo = "piloto", telefones = [] }', liberacao, 1) + teste
    caminho = tmp_path / "politica.toml"
    caminho.write_text(texto, encoding="utf-8")
    (tmp_path / "cardapio-teste.toml").write_text(
        CARDAPIO.read_text(encoding="utf-8") if cardapio is None else cardapio, encoding="utf-8"
    )
    return caminho


def test_sem_o_bloco_teste_nada_muda(politica):
    assert politica.teste is None


def test_o_modo_teste_carrega_o_cardapio_dos_5_casos(tmp_path):
    teste = carregar_politica(_politica_de_teste(tmp_path)).teste
    assert teste.cardapio == tmp_path / "cardapio-teste.toml"
    assert teste.campanha == "teste"
    assert teste.caso_padrao == 1
    assert [(c.numero, c.nome, c.meses_atraso) for c in teste.casos] == [
        (1, "Ana", 3),
        (2, "Bruno", 3),
        (3, "Carla", 4),
        (4, "Daniel", 5),
        (5, "Elisa", 6),
    ]
    assert [len(c.historico) for c in teste.casos] == [3, 3, 3, 3, 0]
    assert {h.forma for h in teste.casos[0].historico} == {FormaHistorico.CARTAO}
    assert {h.forma for h in teste.casos[3].historico} == {
        FormaHistorico.PIX,
        FormaHistorico.BOLETO,
        FormaHistorico.CARTAO,
    }


def test_o_exemplo_comentado_do_politica_toml_vale(tmp_path):
    # O bloco `[teste]` comentado do politica.toml da raiz carrega depois de descomentado.
    linhas = ARQUIVO_PADRAO.read_text(encoding="utf-8").splitlines()
    inicio = linhas.index("# [teste]")
    bloco = "\n".join(
        linha.removeprefix("# ") for linha in linhas[inicio:] if linha.startswith("#")
    )
    caminho = _politica_de_teste(tmp_path, teste="\n" + bloco + "\n")
    assert len(carregar_politica(caminho).teste.casos) == 5


def test_o_numero_do_caso_repetido_e_recusado(tmp_path):
    cardapio = CARDAPIO.read_text(encoding="utf-8").replace("numero = 2", "numero = 1", 1)
    with pytest.raises(PoliticaInvalida, match="casos"):
        carregar_politica(_politica_de_teste(tmp_path, cardapio=cardapio))


def test_os_numeros_do_cardapio_tem_de_comecar_em_1_e_seguir(tmp_path):
    cardapio = CARDAPIO.read_text(encoding="utf-8").replace("numero = 5", "numero = 7", 1)
    with pytest.raises(PoliticaInvalida, match="casos"):
        carregar_politica(_politica_de_teste(tmp_path, cardapio=cardapio))


def test_o_caso_padrao_inexistente_e_recusado(tmp_path):
    teste = TESTE + "caso_padrao = 9\n"
    with pytest.raises(PoliticaInvalida, match="caso_padrao"):
        carregar_politica(_politica_de_teste(tmp_path, teste=teste))


def test_a_campanha_do_teste_nao_pode_ser_a_da_importacao_do_ctn(tmp_path):
    # Com a campanha `ctn` o teste se misturaria com a importação diária da carteira real.
    teste = TESTE + 'campanha = "ctn"\n'
    with pytest.raises(PoliticaInvalida, match="campanha"):
        carregar_politica(_politica_de_teste(tmp_path, teste=teste))


@pytest.mark.parametrize("sem", ["{valor}", "{nome}"])
def test_o_texto_de_abertura_sem_nome_ou_valor_e_recusado(tmp_path, sem):
    teste = TESTE.replace(sem, "")
    with pytest.raises(PoliticaInvalida, match=r"texto_abertura.*" + sem.strip("{}")):
        carregar_politica(_politica_de_teste(tmp_path, teste=teste))


def test_o_texto_de_abertura_sem_mensalidades_e_aceito(tmp_path):
    teste = TESTE.replace(" Há {mensalidades} mensalidades em aberto,", "")
    assert carregar_politica(_politica_de_teste(tmp_path, teste=teste)).teste


def test_o_erro_no_cardapio_diz_o_arquivo_e_o_campo(tmp_path):
    cardapio = CARDAPIO.read_text(encoding="utf-8").replace("meses_atraso = 6", "meses_atraso = 7")
    with pytest.raises(PoliticaInvalida, match=r"cardapio-teste\.toml.*caso\[4\]\.meses_atraso"):
        carregar_politica(_politica_de_teste(tmp_path, cardapio=cardapio))


def test_o_cardapio_com_forma_desconhecida_e_recusado(tmp_path):
    cardapio = CARDAPIO.read_text(encoding="utf-8").replace('"CARTAO"', '"CHEQUE"', 1)
    with pytest.raises(PoliticaInvalida, match=r"caso\[0\]\.historico\.0\.forma"):
        carregar_politica(_politica_de_teste(tmp_path, cardapio=cardapio))


def test_o_cardapio_que_nao_existe_e_recusado(tmp_path):
    caminho = _politica_de_teste(tmp_path)
    (tmp_path / "cardapio-teste.toml").unlink()
    with pytest.raises(PoliticaInvalida, match=r"teste\.cardapio"):
        carregar_politica(caminho)


def test_dois_telefones_do_piloto_com_o_mesmo_final_sao_recusados(tmp_path):
    piloto = 'liberacao = { modo = "piloto", telefones = ["+5583911110001", "+5583922220001"] }'
    with pytest.raises(PoliticaInvalida, match="4 últimos"):
        carregar_politica(_politica_de_teste(tmp_path, liberacao=piloto))
