"""O envio é o último efeito do turno: a intenção é gravada antes e confirmada depois (tarefa 5.3).

Uma falha que vem depois de o Chatwoot entregar não reenvia a mensagem nem a conta duas vezes no
teto; a que vem antes reenvia o mesmo texto, com o mesmo link e o mesmo Pix.
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import Conversa, Mensagem, Pagamento, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    FormaPagamento,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.entrega import entregue
from agente.turno.handoff import devolver_ao_robo
from agente.turno.orquestrador import _descartar, processar_turno

from .auxiliares import _bruto, _deps
from .test_orquestrador_falhas import PixFora
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


def _deps_com(politica, chatwoot, respostas=None):
    """Cada chamada ao Luna devolve a resposta seguinte (o padrão: o Pix, duas vezes)."""
    deps = _deps(politica, [_bruto(), _bruto()] if respostas is None else respostas)
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
    assert len(deps.llm_turno.pedidos) == 1
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
    assert len(deps.llm_turno.pedidos) == 1
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
    # A mesma frase (a do terceiro) sai em dois turnos; a falha do 2º, antes de entregar, não adota
    # o id do 1º.
    terceiro = _bruto(terceiro=True)
    deps = _deps_com(politica, ChatwootInstavel(antes={2}), [terceiro, terceiro])
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


def test_a_mensagem_do_cliente_que_chegou_na_espera_ainda_ganha_o_seu_turno(
    sessao, ligada, politica
):
    # O Pix do 1º turno já vale quando o 2º começa (a pendente o reenvia), então a 2ª resposta
    # vai sem ele.
    respostas = [_bruto(), _bruto(reply="Seu valor em aberto é {divida}.")]
    deps = _deps_com(politica, ChatwootInstavel(antes={1}), respostas)
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


def test_a_pendente_que_o_desistir_adiou_nao_sai_fora_da_janela(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    ligada.falhas_turno = politica.integracao_tentativas
    depois_das_18h = SEGUNDA_10H.replace(hour=18, minute=30)

    assert processar_turno(sessao, ligada.id, depois_das_18h, deps) is ResultadoTurno.ADIADO

    assert deps.chatwoot.enviadas(555) == []
    assert _do_agente(sessao)[0].envio is EstadoEnvio.PENDENTE
    assert ligada.turno_em == SEGUNDA_10H.replace(day=27, hour=8)
    # Aberta a janela, o handoff confere o Chatwoot e descarta o que nunca saiu.
    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.HANDOFF
    assert _do_agente(sessao)[0].envio is EstadoEnvio.DESCARTADA
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def test_a_pendente_que_o_desistir_adiou_nao_e_reenviada_sem_conferir(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(depois={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    ligada.falhas_turno = politica.integracao_tentativas
    depois_das_18h = SEGUNDA_10H.replace(hour=18, minute=30)

    # A mensagem chegou a sair: adiar o handoff não pode mandá-la outra vez.
    assert processar_turno(sessao, ligada.id, depois_das_18h, deps) is ResultadoTurno.ADIADO
    assert len(deps.chatwoot.enviadas(555)) == 1

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.HANDOFF
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "falso-1")


class ChatwootQueNormaliza(ChatwootInstavel):
    """O Chatwoot devolve o texto com outra quebra de linha e espaços a mais."""

    def mensagens_do_agente(self, conversa_id):
        return [
            (chatwoot_id, "  " + texto.replace(" ", "  ").replace("\n", "\r\n") + " \n")
            for chatwoot_id, texto in super().mensagens_do_agente(conversa_id)
        ]


def test_a_conferencia_ignora_espacos_e_quebras_de_linha_que_o_chatwoot_mexe(
    sessao, ligada, politica
):
    deps = _deps_com(politica, ChatwootQueNormaliza(depois={1}))
    _pergunta(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
    resultado = processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert len(deps.chatwoot.enviadas(555)) == 1
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.chatwoot_message_id) == (EstadoEnvio.ENVIADA, "falso-1")


def test_a_conferencia_so_descarta_os_ids_da_propria_conversa(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    [pendente] = _do_agente(sessao)
    # O id "falso-1" é de outra conversa: não é a mensagem desta.
    outra = Conversa(cobranca_id=ligada.cobranca_id, chatwoot_conversa_id=777)
    sessao.add(outra)
    sessao.flush()
    registrar_mensagem(
        sessao, outra.id, OrigemMensagem.AGENTE, "outra", "falso-1", True, SEGUNDA_10H
    )
    deps.chatwoot.mensagens_do_agente = lambda _id: [("falso-1", pendente.texto)]

    assert entregue(sessao, ligada, pendente, deps.chatwoot) == "falso-1"


def test_um_erro_de_codigo_no_turno_conta_como_erro_interno_e_nao_como_falha_de_integracao(
    sessao, ligada, politica
):
    deps = _deps_com(politica, ChatwootInstavel(), [KeyError("bug")])
    _pergunta(sessao, ligada)
    sessao.commit()

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    assert (ligada.falhas_turno, ligada.erros_turno) == (0, 1)
    [erro] = sessao.scalars(select(Turno)).all()
    assert erro.redacao == {"erro": "KeyError", "tentativa": 1}


def _chatwoot_com_bug_na_nota():
    chatwoot = ChatwootInstavel()

    def nota_privada(conversa_id, texto):
        raise KeyError("bug")

    chatwoot.nota_privada = nota_privada
    return chatwoot


def test_um_erro_de_codigo_no_handoff_das_tentativas_conta_como_erro_interno(
    sessao, ligada, politica
):
    deps = _deps_com(politica, _chatwoot_com_bug_na_nota())
    _pergunta(sessao, ligada)
    ligada.falhas_turno = politica.integracao_tentativas
    sessao.commit()

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    assert (ligada.falhas_turno, ligada.erros_turno) == (politica.integracao_tentativas, 1)
    assert ligada.turno_em is not None


def test_um_erro_de_codigo_no_handoff_da_ultima_falha_conta_como_erro_interno(
    sessao, ligada, politica
):
    deps = _deps_com(politica, _chatwoot_com_bug_na_nota())
    deps.pix = PixFora(10**6)
    _pergunta(sessao, ligada)
    ligada.falhas_turno = politica.integracao_tentativas - 1
    sessao.commit()

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    assert ligada.erros_turno == 1


def _turnos(sessao, resultado):
    return sessao.scalars(select(Turno).where(Turno.resultado == resultado)).all()


def test_o_turno_cuja_mensagem_o_humano_cancelou_deixa_de_dizer_enviado(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    ligada.human_owned = True
    processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    [turno] = _turnos(sessao, ResultadoTurno.DESCARTADO)
    assert turno.texto_enviado is None
    assert turno.redacao["envio"] == "DESCARTADA"
    assert _turnos(sessao, ResultadoTurno.ENVIADO) == []


def test_o_turno_cuja_mensagem_o_desistir_descartou_deixa_de_dizer_enviado(
    sessao, ligada, politica
):
    n = politica.integracao_tentativas
    deps = _deps_com(politica, ChatwootInstavel(antes=set(range(1, n + 1))))
    _pergunta(sessao, ligada)
    agora = SEGUNDA_10H
    for _ in range(n):
        processar_turno(sessao, ligada.id, agora, deps)
        agora = ligada.turno_em or agora

    [turno] = _turnos(sessao, ResultadoTurno.DESCARTADO)
    assert turno.texto_enviado is None


def test_o_turno_cuja_mensagem_saiu_continua_enviado(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(depois={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    processar_turno(sessao, ligada.id, ligada.turno_em, deps)

    [turno] = _turnos(sessao, ResultadoTurno.ENVIADO)
    assert turno.texto_enviado == _do_agente(sessao)[0].texto
    assert _turnos(sessao, ResultadoTurno.DESCARTADO) == []


def _notas(chatwoot):
    return [chamada[2] for chamada in chatwoot.chamadas if chamada[0] == "nota_privada"]


def test_a_pendente_que_passou_das_24h_do_whatsapp_vai_ao_atendente_com_nota(
    sessao, ligada, politica
):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    # O retorno só vem no dia seguinte à tarde: a última mensagem do cliente passou das 24h.
    mais_de_24h = SEGUNDA_10H + timedelta(hours=25)

    assert processar_turno(sessao, ligada.id, mais_de_24h, deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == []
    [mensagem] = _do_agente(sessao)
    assert (mensagem.envio, mensagem.conta_no_teto) == (EstadoEnvio.DESCARTADA, False)
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.FORA_DAS_24H,
    )
    [nota] = _notas(deps.chatwoot)
    assert "passaram as 24h do WhatsApp" in nota
    assert (ligada.turno_em, ligada.falhas_turno) == (None, 0)
    [turno] = _turnos(sessao, ResultadoTurno.HANDOFF)
    assert turno.acao_principal == "HUMAN:FORA_DAS_24H"
    # O turno que gravou a resposta deixa de dizer que ela saiu.
    assert _turnos(sessao, ResultadoTurno.ENVIADO) == []


def test_a_pendente_fora_das_24h_fica_pendente_se_o_chatwoot_falha_na_nota(
    sessao, ligada, politica
):
    class SemNota(ChatwootInstavel):
        def nota_privada(self, conversa_id, texto):
            raise httpx.ConnectError("o Chatwoot não respondeu")

    deps = _deps_com(politica, SemNota(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H + timedelta(hours=25), deps)

    # O descarte é desfeito: a pendente volta a esperar, e a falha conta como de integração.
    assert resultado is ResultadoTurno.ERRO
    assert _do_agente(sessao)[0].envio is EstadoEnvio.PENDENTE
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert ligada.falhas_turno == 2


def test_a_pendente_barrada_pelo_teto_e_descartada(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    for _ in range(politica.teto_resolucao):
        registrar_mensagem(
            sessao, ligada.id, OrigemMensagem.AGENTE, "outra", None, True, SEGUNDA_10H
        )

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.enviadas(555) == []
    assert _do_agente(sessao)[0].envio is EstadoEnvio.DESCARTADA
    assert (ligada.turno_em, ligada.falhas_turno) == (None, 0)


def test_a_pendente_que_e_a_ultima_do_teto_ainda_sai(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    # A 5ª mensagem conta no teto e continua valendo: o teto só barra a 6ª.
    for _ in range(politica.teto_resolucao - 1):
        registrar_mensagem(
            sessao, ligada.id, OrigemMensagem.AGENTE, "outra", None, True, SEGUNDA_10H
        )
    assert mensagens_no_teto(sessao, ligada.id) == politica.teto_resolucao

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.ENVIADO


def test_o_descarte_da_pendente_zera_a_conta_de_falhas(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes={1, 2}))
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    processar_turno(sessao, ligada.id, ligada.turno_em, deps)
    assert ligada.falhas_turno == 2
    ligada.human_owned = True

    assert processar_turno(sessao, ligada.id, ligada.turno_em, deps) is ResultadoTurno.IGNORADO

    assert ligada.falhas_turno == 0


@pytest.mark.parametrize("caminho", ["tentativas_esgotadas", "humano_assume"])
def test_a_chave_descartada_nao_conta_como_meio_enviado_quando_o_robo_reassume(
    sessao, ligada, politica, caminho
):
    n = politica.integracao_tentativas
    antes = set(range(1, n + 1)) if caminho == "tentativas_esgotadas" else {1}
    deps = _deps_com(politica, ChatwootInstavel(antes=antes), [_bruto(), _bruto(), _bruto()])
    _pergunta(sessao, ligada)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    if caminho == "tentativas_esgotadas":
        agora = ligada.turno_em
        for _ in range(n - 1):
            processar_turno(sessao, ligada.id, agora, deps)
            agora = ligada.turno_em or agora
        devolver_ao_robo(sessao, ligada, agora)
    else:
        agora = ligada.turno_em
        ligada.human_owned = True
        assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.IGNORADO
        devolver_ao_robo(sessao, ligada, agora)
    [descartada] = _do_agente(sessao)[:1]
    assert descartada.envio is EstadoEnvio.DESCARTADA
    [pix] = deps.pix.cobrancas.values()
    assert pix.br_code in descartada.texto
    assert len(sessao.scalars(select(Pagamento)).all()) == 1

    deps.chatwoot.antes.clear()  # o Chatwoot voltou
    mais_tarde = agora + timedelta(minutes=30)
    _pergunta(sessao, ligada, mais_tarde, mid="cw-2")
    assert processar_turno(sessao, ligada.id, mais_tarde, deps) is ResultadoTurno.ENVIADO

    # A chave que nunca saiu não vale como enviada: o texto novo a leva, e é o mesmo Pix.
    assert pix.br_code in deps.chatwoot.enviadas(555)[-1]
    assert len(sessao.scalars(select(Pagamento)).all()) == 1
    assert len(deps.pix.cobrancas) == 1


def test_descartar_mensagem_sem_meio_nao_apaga_o_meio_que_ja_saiu(sessao, ligada, politica):
    deps = _deps_com(politica, ChatwootInstavel(antes=set()), [_bruto()])
    _pergunta(sessao, ligada)
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ENVIADO
    assert ligada.forma_enviada is FormaPagamento.PIX
    # Uma mensagem do agente sem Pix nem link, gerada por um turno sem `forma`, fica pendente.
    sem_meio = registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.AGENTE, "sem meio", None, True, SEGUNDA_10H
    )
    sessao.add(
        Turno(
            conversa_id=ligada.id,
            modo=Modo.ATIVO,
            resultado=ResultadoTurno.ENVIADO,
            texto_enviado="sem meio",
            numeros={"acoes": ["ANSWER"]},
            redacao={},
        )
    )
    sessao.flush()

    _descartar(sessao, sem_meio)

    assert ligada.forma_enviada is FormaPagamento.PIX
