from datetime import date, time, timedelta

import pytest

from agente.politica import PoliticaInvalida, Regua, carregar_politica
from agente.tipos import MotivoHumano

from .conftest import RAIZ

ARQUIVO_PADRAO = RAIZ / "politica.toml"


def test_arquivo_padrao_carrega(politica):
    assert politica.mensalidade_centavos == 3340
    assert politica.teto_resolucao == 5
    assert politica.regua_acima_de_6 is Regua.R6
    assert politica.confianca_minima == 0.60


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


def test_propensao_e_limiares(politica):
    assert politica.propensao.n_meses == 3
    assert politica.propensao.tolerancia_dias == 3
    assert politica.limiares_jev1["quer_retencao"] == 0.80
    assert politica.limiares_jev2["GENERATE_PIX"] == 0.90


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
        ("quer_retencao = 0.80", "quer_retencao = 1.5", "quer_retencao"),
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


def test_parametros_do_webchat_tem_padrao(politica):
    assert politica.webchat_token_validade_horas == 720
    assert politica.webchat_cpf_digitos == 3
    assert politica.webchat_cpf_tentativas == 3
    assert politica.webchat_cpf_bloqueio_horas == 1
    assert politica.webchat_verificacao_validade_minutos == 120


def test_a_saida_no_whatsapp_tem_padrao(politica):
    assert politica.motivos_humano_no_whatsapp == {MotivoHumano.DESCONFIANCA}
    assert politica.texto_humano_no_whatsapp == (
        "Entendo a sua preocupação. Um atendente da unidade vai continuar com você aqui mesmo, "
        "nesta conversa."
    )
    assert politica.limiares_jev1["desconfia_golpe"] == 0.70


def test_motivo_desconhecido_na_saida_no_whatsapp_e_recusado(tmp_path):
    caminho = _arquivo_com(
        tmp_path,
        'motivos_humano_no_whatsapp = ["DESCONFIANCA"]',
        'motivos_humano_no_whatsapp = ["DESCONFIANCA", "NAO_EXISTE"]',
    )
    with pytest.raises(PoliticaInvalida, match="motivos_humano_no_whatsapp"):
        carregar_politica(caminho)


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


def test_limiar_do_jev1_faltando_e_recusado(tmp_path):
    caminho = _arquivo_com(tmp_path, "desconfia_golpe = 0.70", "")
    with pytest.raises(PoliticaInvalida, match="limiares_jev1.*desconfia_golpe"):
        carregar_politica(caminho)


def test_o_teto_no_whatsapp_e_recusado(tmp_path):
    caminho = _arquivo_com(
        tmp_path,
        'motivos_humano_no_whatsapp = ["DESCONFIANCA"]',
        'motivos_humano_no_whatsapp = ["DESCONFIANCA", "TETO"]',
    )
    with pytest.raises(PoliticaInvalida, match="motivos_humano_no_whatsapp.*TETO"):
        carregar_politica(caminho)
