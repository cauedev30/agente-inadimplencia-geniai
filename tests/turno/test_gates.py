from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agente.db.historico import registrar_mensagem
from agente.tipos import EstadoConversa, OrigemMensagem, StatusCobranca, TipoFixa
from agente.turno.gates import Adiar, Fixa, Ignorar, Reiniciar, Retomar, Seguir, avaliar_gates

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


def test_em_sombra_o_humano_na_conversa_nao_barra(sessao, conversa, politica):
    conversa.human_owned = True
    mensagens = [_cliente(sessao, conversa)]
    resultado = avaliar_gates(
        conversa, mensagens, SEGUNDA_10H, politica, sessao, barrar_humano=False
    )
    assert resultado == Seguir()


def test_reinicio_so_para_telefone_da_equipe(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto=politica.comando_reinicio)
    assert _avaliar(sessao, conversa, [mensagem], politica) == Seguir()

    da_equipe = politica.model_copy(
        update={"telefones_equipe": frozenset({conversa.cobranca.cliente.telefone})}
    )
    assert _avaliar(sessao, conversa, [mensagem], da_equipe) == Reiniciar()


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


def test_comprovante_em_imagem_segue(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, texto="", anexos=[{"tipo": "image"}])
    assert _avaliar(sessao, conversa, [mensagem], politica) == Seguir()


def test_fora_da_janela_adia_ate_a_abertura(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=DOMINGO)
    resultado = _avaliar(sessao, conversa, [mensagem], politica, agora=DOMINGO)
    assert resultado == Adiar(datetime(2026, 11, 3, 8, tzinfo=JP))  # segunda é Finados


def test_passou_das_24h_do_whatsapp_vira_retomada(sessao, conversa, politica):
    mensagem = _cliente(sessao, conversa, momento=SEGUNDA_10H - timedelta(hours=25))
    assert _avaliar(sessao, conversa, [mensagem], politica) == Retomar()


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
