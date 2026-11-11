from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import registrar_mensagem
from agente.tipos import EstadoConversa, OrigemMensagem, StatusCobranca, TipoFixa
from agente.turno.gates import (
    Adiar,
    ComprovanteSemTexto,
    Fixa,
    ForaDas24h,
    Ignorar,
    Quitada,
    Reiniciar,
    Seguir,
    avaliar_gates,
)

JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)
DOMINGO = datetime(2026, 11, 1, 10, tzinfo=JP)


def _cliente(sessao, conversa, texto="oi", momento=SEGUNDA_10H, anexos=None, mid="cw-1"):
    return registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, texto, mid, False, momento, anexos=anexos
    )


def _agente(sessao, conversa, quantas):
    for _ in range(quantas):
        registrar_mensagem(
            sessao, conversa.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
        )


def _avaliar(sessao, conversa, mensagens, politica, agora=SEGUNDA_10H):
    return avaliar_gates(conversa, mensagens, agora, politica, sessao)


def test_caminho_livre_segue(sessao, conversa, politica):
    assert _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica) == Seguir()


def test_cobranca_que_nao_esta_ativa_e_ignorada(sessao, conversa, politica):
    conversa.cobranca.status = StatusCobranca.PAGA
    resultado = _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Ignorar("sem_cobranca_ativa")


@pytest.mark.parametrize("como", ["human_owned", "estado"])
def test_humano_na_conversa_e_ignorado(sessao, conversa, politica, como):
    if como == "human_owned":
        conversa.human_owned = True
    else:
        conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
    resultado = _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Ignorar("humano_na_conversa")


def test_reinicio_so_para_telefone_da_equipe(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto=politica.comando_reinicio)
    assert _avaliar(sessao, conversa, [mensagem], politica) == Seguir()

    da_equipe = politica.model_copy(
        update={"telefones_equipe": frozenset({conversa.cobranca.cliente.telefone})}
    )
    assert _avaliar(sessao, conversa, [mensagem], da_equipe) == Reiniciar()


def test_reinicio_da_equipe_casa_com_e_sem_o_9(sessao, conversa, politica):
    """A1: a equipe está na política com o 9 e a carteira guarda sem ele (e vice-versa)."""
    mensagem = _cliente(sessao, conversa, texto=politica.comando_reinicio)
    com_9 = "+5583999990001"
    sem_9 = "+558399990001"
    for guardado, da_equipe in ((sem_9, com_9), (com_9, sem_9)):
        conversa.cobranca.cliente.telefone = guardado
        politica_da_equipe = politica.model_copy(
            update={"telefones_equipe": frozenset({da_equipe})}
        )
        assert _avaliar(sessao, conversa, [mensagem], politica_da_equipe) == Reiniciar()


@pytest.mark.parametrize("como", ["human_owned", "estado"])
def test_reinicio_da_equipe_vale_com_o_humano_na_conversa(sessao, conversa, politica, como):
    if como == "human_owned":
        conversa.human_owned = True
    else:
        conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
    mensagem = _cliente(sessao, conversa, texto=f"  {politica.comando_reinicio.upper()} ")
    assert _avaliar(sessao, conversa, [mensagem], politica) == Ignorar("humano_na_conversa")

    da_equipe = politica.model_copy(
        update={"telefones_equipe": frozenset({conversa.cobranca.cliente.telefone})}
    )
    assert _avaliar(sessao, conversa, [mensagem], da_equipe) == Reiniciar()


def test_rajada_so_com_anexo_nao_aceito_e_ignorada(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto="", anexos=[{"tipo": "video"}])
    assert _avaliar(sessao, conversa, [mensagem], politica) == Ignorar("anexo_nao_aceito")


@pytest.mark.parametrize("tipo", ["image", "file"])
def test_so_imagem_ou_arquivo_sem_texto_vai_ao_humano(sessao, conversa, politica, tipo):
    mensagem = _cliente(sessao, conversa, texto="", anexos=[{"tipo": tipo}])
    assert _avaliar(sessao, conversa, [mensagem], politica) == ComprovanteSemTexto()


def test_imagem_com_texto_na_rajada_segue(sessao, conversa, politica):
    foto = _cliente(sessao, conversa, texto="", anexos=[{"tipo": "image"}])
    texto = _cliente(sessao, conversa, texto="segue o comprovante", mid="cw-2")
    assert _avaliar(sessao, conversa, [foto, texto], politica) == Seguir()
    legenda = _cliente(sessao, conversa, texto="segue", anexos=[{"tipo": "image"}], mid="cw-3")
    assert _avaliar(sessao, conversa, [legenda], politica) == Seguir()


def test_imagem_com_audio_sem_texto_segue_para_o_audio_ser_ouvido(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto="", anexos=[{"tipo": "image"}, {"tipo": "audio"}])
    assert _avaliar(sessao, conversa, [mensagem], politica) == Seguir()


def test_imagem_sem_texto_fora_da_janela_espera_a_abertura(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto="", anexos=[{"tipo": "image"}], momento=DOMINGO)
    resultado = _avaliar(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert resultado == Adiar(datetime(2026, 11, 3, 8, tzinfo=JP))


def test_fora_da_janela_adia_ate_a_abertura(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=DOMINGO)
    resultado = _avaliar(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert resultado == Adiar(datetime(2026, 11, 3, 8, tzinfo=JP))  # segunda é Finados


def test_passou_das_24h_do_whatsapp_vira_fora_das_24h(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=SEGUNDA_10H - timedelta(hours=25))
    assert _avaliar(sessao, conversa, [mensagem], politica) == ForaDas24h()


def test_no_teto_sai_a_6a_fixa(sessao, conversa, politica):
    _agente(sessao, conversa, politica.teto_resolucao)
    resultado = _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Fixa(TipoFixa.CANAL_EXTERNO)


def test_abaixo_do_teto_segue(sessao, conversa, politica):
    _agente(sessao, conversa, politica.teto_resolucao - 1)
    assert _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica) == Seguir()


def test_no_teto_a_fixa_sai_mesmo_depois_de_outra_fixa(sessao, conversa, politica):
    # Uma fixa anterior (a de desconfiança, por exemplo) não encerra o teto: quem decide o que
    # sai na 6ª, ou se nada sai, é o orquestrador.
    _agente(sessao, conversa, politica.teto_resolucao)
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.FIXA, "canal externo", None, False, SEGUNDA_10H
    )
    resultado = _avaliar(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Fixa(TipoFixa.CANAL_EXTERNO)


def test_ordem_humano_vem_antes_da_janela(sessao, conversa, politica):
    conversa.human_owned = True
    mensagem = _cliente(sessao, conversa, momento=DOMINGO)
    resultado = _avaliar(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert resultado == Ignorar("humano_na_conversa")


def test_ordem_janela_vem_antes_do_teto(sessao, conversa, politica):
    _agente(sessao, conversa, politica.teto_resolucao)
    mensagem = _cliente(sessao, conversa, momento=DOMINGO)
    resultado = _avaliar(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert isinstance(resultado, Adiar)


def test_rajada_vazia_e_ignorada(sessao, conversa, politica):
    assert _avaliar(sessao, conversa, [], politica) == Ignorar("sem_mensagem")


def _quitada(sessao, conversa, mensagens, politica, agora=SEGUNDA_10H, **resto):
    return avaliar_gates(conversa, mensagens, agora, politica, sessao, quitada_no_ctn=True, **resto)


def test_quitada_no_ctn_e_o_ultimo_gate_antes_do_seguir(sessao, conversa, politica):
    assert _quitada(sessao, conversa, [_cliente(sessao, conversa)], politica) == Quitada()


def test_quitada_no_ctn_tambem_vale_para_o_toque_no_botao(sessao, conversa, politica):
    # Quem quitou não gera Pix: o toque no botão cai no mesmo gate.
    mensagem = _cliente(sessao, conversa, texto=BOTAO_PAGAR_PIX.texto)
    assert _quitada(sessao, conversa, [mensagem], politica) == Quitada()


def test_quitada_no_ctn_com_humano_na_conversa_nao_faz_nada(sessao, conversa, politica):
    conversa.human_owned = True
    resultado = _quitada(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Ignorar("humano_na_conversa")


def test_quitada_no_ctn_fora_da_janela_de_atendimento_adia(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=DOMINGO)
    resultado = _quitada(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert isinstance(resultado, Adiar)


def test_quitada_no_ctn_passou_das_24h_vai_ao_humano_sem_mensagem(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=SEGUNDA_10H - timedelta(hours=25))
    assert _quitada(sessao, conversa, [mensagem], politica) == ForaDas24h()


def test_quitada_no_ctn_no_teto_segue_o_gate_do_teto(sessao, conversa, politica):
    _agente(sessao, conversa, politica.teto_resolucao)
    resultado = _quitada(sessao, conversa, [_cliente(sessao, conversa)], politica)
    assert resultado == Fixa(TipoFixa.CANAL_EXTERNO)


def test_quitada_no_ctn_com_reinicio_da_equipe_reinicia(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto=politica.comando_reinicio)
    da_equipe = politica.model_copy(
        update={"telefones_equipe": frozenset({conversa.cobranca.cliente.telefone})}
    )
    assert _quitada(sessao, conversa, [mensagem], da_equipe) == Reiniciar()
