"""O envio é o último efeito do turno: a intenção é gravada antes e confirmada depois (tarefa 5.3).

Uma falha que vem depois de o Chatwoot entregar não reenvia a mensagem nem a conta duas vezes no
teto; a que vem antes reenvia o mesmo texto, com o mesmo link e o mesmo Pix.
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import Mensagem, Pagamento, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.jev import responder
from agente.integracoes.redator import RedatorFalso
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, TEXTO_RESPOSTA_SEGURA
from agente.turno.orquestrador import _ja_saiu_resposta_segura, processar_turno

from .test_orquestrador_ativo import _deps
from .test_orquestrador_gates import SEGUNDA_10H


class ChatwootInstavel(ChatwootFalso):
    """Falha no envio de certas chamadas (contadas de 1): antes de entregar ou depois."""

    def __init__(self, antes=(), depois=(), ao_enviar=None):
        super().__init__()
        self.antes, self.depois, self.ao_enviar = set(antes), set(depois), ao_enviar
        self.envios = 0

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        self.envios += 1
        if self.ao_enviar is not None:
            self.ao_enviar()
        if self.envios in self.antes:
            raise httpx.ConnectError("o Chatwoot não respondeu")
        chatwoot_id = super().enviar_mensagem(conversa_id, texto, botoes)
        if self.envios in self.depois:
            raise httpx.ReadTimeout("a resposta do Chatwoot se perdeu")
        return chatwoot_id


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _pergunta(sessao, conversa, momento=SEGUNDA_10H, mid="cw-1"):
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, "quanto eu devo?", mid, False, momento
    )


def _do_agente(sessao):
    consulta = select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    return sessao.scalars(consulta.order_by(Mensagem.id)).all()


def _deps_com(politica, chatwoot, regras=(), redator=None):
    deps = _deps(politica, regras, redator=redator or RedatorFalso(registrar=True))
    deps.chatwoot = chatwoot
    return deps


def test_falha_depois_de_entregar_nao_reenvia_nem_conta_duas_vezes(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(depois={1}))
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.ENVIADO
    [texto] = deps.chatwoot.enviadas(555)
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.envio) == (texto, EstadoEnvio.ENVIADA)
    assert mensagem.chatwoot_message_id == "falso-1"
    assert mensagens_no_teto(sessao, ligada.id) == 1
    # Nada do turno se refez: um Pix, um pedido ao LLM, uma conta na conversa.
    assert len(deps.pix.cobrancas) == 1 and len(sessao.scalars(select(Pagamento)).all()) == 1
    assert len(deps.redator.pedidos) == 1
    assert (ligada.falhas_turno, ligada.turno_em) == (0, None)
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_falha_antes_de_entregar_reenvia_o_mesmo_texto_com_o_mesmo_pix(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
    assert deps.chatwoot.enviadas(555) == []
    texto = _do_agente(sessao)[0].texto
    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [texto]
    [pix] = deps.pix.cobrancas.values()
    assert pix.br_code in texto
    assert len(_do_agente(sessao)) == 1 and mensagens_no_teto(sessao, ligada.id) == 1
    assert len(deps.redator.pedidos) == 1
    assert deps.pix.cobrancas.keys() == {p.referencia for p in sessao.scalars(select(Pagamento))}


def test_a_intencao_esta_gravada_quando_o_chatwoot_recebe_o_envio(sessao, ligada, politica):
    vistos = []

    def espiar():
        [mensagem] = _do_agente(sessao)
        vistos.append((mensagem.envio, mensagem.chatwoot_message_id, mensagem.conta_no_teto))
        vistos.append((ligada.estado, ligada.turno_em is not None))

    deps = _deps_com(politica, ChatwootInstavel(ao_enviar=espiar))
    _pergunta(sessao, ligada)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # No envio, o turno já valeu no banco e há um retorno agendado, para o caso de o processo cair.
    assert vistos == [
        (EstadoEnvio.PENDENTE, None, True),
        (EstadoConversa.AGUARDANDO_PAGAMENTO, True),
    ]
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "falso-1")
    assert ligada.turno_em is None
    assert sessao.scalars(select(Turno)).one().texto_enviado == mensagem.texto


def test_o_texto_igual_a_um_ja_entregue_nao_e_confundido_com_ele(sessao, ligada, politica):
    # A mesma frase sai em dois turnos; a falha do 2º, antes de entregar, não adota o id do 1º.
    fora_de_escopo = [responder("acao", "OUT_OF_SCOPE")]
    deps = _deps_com(politica, ChatwootInstavel(antes={2}), fora_de_escopo)
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    agora = SEGUNDA_10H + timedelta(minutes=1)
    _pergunta(sessao, ligada, agora, "cw-2")
    assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.ERRO
    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO

    primeira, segunda = _do_agente(sessao)
    assert primeira.texto == segunda.texto
    assert (primeira.chatwoot_message_id, segunda.chatwoot_message_id) == ("falso-1", "falso-2")
    assert len(deps.chatwoot.enviadas(555)) == 2


def test_esgotadas_as_tentativas_o_texto_nunca_enviado_e_descartado(sessao, ligada, politica):
    n = politica.integracao_tentativas
    deps = _deps_com(politica, ChatwootInstavel(antes=set(range(1, n + 1))))
    _pergunta(sessao, ligada)

    agora, resultados = SEGUNDA_10H, []
    for _ in range(n):
        resultados.append(processar_turno(sessao, ligada.id, agora, deps))
        agora = ligada.turno_em or agora

    assert resultados == [ResultadoTurno.ERRO] * (n - 1) + [ResultadoTurno.HANDOFF]
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.conta_no_teto) == (EstadoEnvio.DESCARTADA, False)
    assert mensagem.chatwoot_message_id is None
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert mensagens_no_teto(sessao, ligada.id) == 0
    assert ligada.motivo_humano is MotivoHumano.INTEGRACAO_INDISPONIVEL
    assert len(deps.pix.cobrancas) == 1


def test_esgotadas_as_tentativas_a_mensagem_que_chegou_a_sair_e_adotada(sessao, ligada, politica):
    n = politica.integracao_tentativas
    deps = _deps_com(politica, ChatwootInstavel(depois={1}))
    _pergunta(sessao, ligada)
    ligada.falhas_turno = n - 1

    # A falha depois de entregar é a n-ésima: o handoff confere o Chatwoot antes de descartar.
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF

    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "falso-1")
    assert mensagens_no_teto(sessao, ligada.id) == 1


def test_o_humano_que_assume_na_espera_cancela_o_envio_pendente(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    ligada.human_owned = True

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.enviadas(555) == []
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.conta_no_teto) == (EstadoEnvio.DESCARTADA, False)
    assert ligada.turno_em is None


def test_o_reenvio_que_cai_fora_da_janela_espera_ela_abrir(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    ultimo_minuto = SEGUNDA_10H.replace(hour=17, minute=59, second=30)
    _pergunta(sessao, ligada, ultimo_minuto)
    processar_turno(sessao, ligada.id, ultimo_minuto, deps)

    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.ADIADO
    assert ligada.turno_em == SEGUNDA_10H.replace(day=27, hour=8)
    assert deps.chatwoot.enviadas(555) == []
    assert _do_agente(sessao)[0].envio is EstadoEnvio.PENDENTE
    assert ligada.falhas_turno == 1


def test_a_resposta_segura_descartada_nao_conta_como_ja_enviada(sessao, ligada, politica):
    registrar_mensagem(
        sessao,
        ligada.id,
        OrigemMensagem.AGENTE,
        TEXTO_RESPOSTA_SEGURA,
        None,
        False,
        SEGUNDA_10H,
        envio=EstadoEnvio.DESCARTADA,
    )
    assert _ja_saiu_resposta_segura(sessao, ligada.id) is False


def test_a_mensagem_do_cliente_que_chegou_na_espera_ainda_ganha_o_seu_turno(
    sessao, ligada, politica
):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # O cliente escreve de novo na espera; a rajada dele passa a ser o próximo turno.
    chegou = SEGUNDA_10H + timedelta(seconds=10)
    _pergunta(sessao, ligada, chegou, "cw-2")
    ligada.turno_em = chegou + timedelta(seconds=politica.rajada_segundos)

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO

    assert len(deps.chatwoot.enviadas(555)) == 1
    assert ligada.turno_em is not None
    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO
    assert len(deps.chatwoot.enviadas(555)) == 2
