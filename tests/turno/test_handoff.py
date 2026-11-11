from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, transicionar
from agente.db.modelos import Mensagem, Pagamento
from agente.integracoes.chatwoot import ChatwootFalso
from agente.tipos import (
    EstadoConversa,
    FormaPagamento,
    Laudo,
    MotivoHumano,
    OrigemMensagem,
    StatusPagamento,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO
from agente.turno.handoff import devolver_ao_robo, fazer_handoff

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)
LAUDOS = [Laudo("risco_juridico", True, 0.91, 0.80)]


def _handoff(sessao, conversa, politica, chatwoot, time_id=4):
    conversa.chatwoot_conversa_id = 555
    fazer_handoff(
        sessao, chatwoot, conversa, MotivoHumano.JURIDICO, LAUDOS, politica, time_id, AGORA
    )


def test_handoff_faz_as_seis_etapas_na_ordem(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff(sessao, conversa, politica, chatwoot)

    assert [chamada[0] for chamada in chatwoot.chamadas] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
        "enviar_mensagem",
    ]
    nota = chatwoot.chamadas[0][2]
    assert "JURIDICO" in nota and "risco_juridico" in nota and "0.91" in nota
    assert chatwoot.chamadas[1][2] == ["juridico"]
    assert chatwoot.chamadas[2][2] == 4
    assert chatwoot.chamadas[3][2] == "open"
    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert chatwoot.chamadas[-1][3] == []
    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert conversa.motivo_humano is MotivoHumano.JURIDICO


def test_mensagem_do_handoff_e_fixa_e_fora_do_teto(sessao, conversa, politica):
    _handoff(sessao, conversa, politica, ChatwootFalso())

    mensagem = sessao.scalars(select(Mensagem)).one()
    assert mensagem.origem is OrigemMensagem.FIXA
    assert mensagem.chatwoot_message_id == "falso-5"
    assert mensagens_no_teto(sessao, conversa.id) == 0


def test_sem_time_configurado_nao_atribui(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff(sessao, conversa, politica, chatwoot, time_id=None)

    assert "atribuir" not in [chamada[0] for chamada in chatwoot.chamadas]


def test_pendente_devolve_ao_robo_no_estado_de_antes(sessao, conversa, politica):
    transicionar(sessao, conversa, EstadoConversa.AGUARDANDO_PAGAMENTO, None, None, momento=AGORA)
    _handoff(sessao, conversa, politica, ChatwootFalso())
    conversa.human_owned = True

    devolver_ao_robo(sessao, conversa, AGORA)

    assert conversa.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert conversa.human_owned is False
    assert conversa.motivo_humano is None


def _pix_pendente(conversa):
    pagamento = Pagamento(
        forma=FormaPagamento.PIX,
        valor_centavos=conversa.cobranca.divida_centavos,
        referencia="tx-1",
        status=StatusPagamento.PENDENTE,
        criado_em=AGORA,
        expira_em=AGORA + timedelta(hours=24),
    )
    conversa.cobranca.pagamentos.append(pagamento)
    return pagamento


def test_por_padrao_o_handoff_deixa_o_pix_pendente_valendo(sessao, conversa, politica):
    assert politica.handoff_expira_pix is False
    pix = _pix_pendente(conversa)

    _handoff(sessao, conversa, politica, ChatwootFalso())

    assert pix.status is StatusPagamento.PENDENTE


def test_com_handoff_expira_pix_o_handoff_expira_o_pendente(sessao, conversa, politica):
    pix = _pix_pendente(conversa)
    pago = _pix_pendente(conversa)
    pago.referencia, pago.status = "tx-2", StatusPagamento.PAGO
    politica = politica.model_copy(update={"handoff_expira_pix": True})

    _handoff(sessao, conversa, politica, ChatwootFalso())

    assert (pix.status, pago.status) == (StatusPagamento.EXPIRADO, StatusPagamento.PAGO)


def test_desconfianca_manda_a_fixa_do_golpe_sem_botao(sessao, conversa, politica):
    conversa.chatwoot_conversa_id = 555
    chatwoot = ChatwootFalso()

    fazer_handoff(
        sessao,
        chatwoot,
        conversa,
        MotivoHumano.DESCONFIANCA,
        [Laudo("desconfia_golpe", True, 0.91, 0.80)],
        politica,
        4,
        AGORA,
    )

    # Tudo igual ao handoff de sempre: nota, etiqueta, atribuição, status, mensagem.
    assert [chamada[0] for chamada in chatwoot.chamadas] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
        "enviar_mensagem",
    ]
    assert (
        "DESCONFIANCA" in chatwoot.chamadas[0][2] and "desconfia_golpe" in chatwoot.chamadas[0][2]
    )
    assert chatwoot.chamadas[1][2] == ["desconfianca"]
    assert chatwoot.chamadas[2][2] == 4
    assert chatwoot.chamadas[3][2] == "open"
    assert chatwoot.enviadas(555) == [politica.texto_humano_no_whatsapp]
    assert chatwoot.chamadas[-1][3] == []
    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert conversa.motivo_humano is MotivoHumano.DESCONFIANCA
    mensagem = sessao.scalars(select(Mensagem)).one()
    assert mensagem.origem is OrigemMensagem.FIXA
    assert mensagem.texto == politica.texto_humano_no_whatsapp
    assert mensagens_no_teto(sessao, conversa.id) == 0


def test_desconfianca_ainda_expira_o_pix_quando_a_politica_manda(sessao, conversa, politica):
    pix = _pix_pendente(conversa)
    conversa.chatwoot_conversa_id = 555
    politica = politica.model_copy(update={"handoff_expira_pix": True})

    fazer_handoff(
        sessao, ChatwootFalso(), conversa, MotivoHumano.DESCONFIANCA, [], politica, None, AGORA
    )

    assert pix.status is StatusPagamento.EXPIRADO


def test_os_outros_motivos_mandam_o_canal_externo_sem_botao(sessao, conversa, politica):
    chatwoot = ChatwootFalso()
    conversa.chatwoot_conversa_id = 555

    for motivo in (MotivoHumano.CONTESTACAO, MotivoHumano.TETO):
        fazer_handoff(sessao, chatwoot, conversa, motivo, [], politica, None, AGORA)

    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO, TEXTO_CANAL_EXTERNO]
    assert [c[3] for c in chatwoot.chamadas if c[0] == "enviar_mensagem"] == [[], []]


TEXTO_REDIGIDO = "Entendi, você falou do desemprego. Passei a conversa para a nossa equipe."


def test_o_texto_redigido_troca_o_da_fixa_do_golpe_no_whatsapp(sessao, conversa, politica):
    chatwoot = ChatwootFalso()
    conversa.chatwoot_conversa_id = 555

    fazer_handoff(
        sessao,
        chatwoot,
        conversa,
        MotivoHumano.DESCONFIANCA,
        [],
        politica,
        None,
        AGORA,
        texto=TEXTO_REDIGIDO,
    )

    assert chatwoot.enviadas(555) == [TEXTO_REDIGIDO]
    assert chatwoot.chamadas[-1][3] == []
    mensagem = sessao.scalars(select(Mensagem)).one()
    assert (mensagem.origem, mensagem.texto) == (OrigemMensagem.FIXA, TEXTO_REDIGIDO)


def test_o_texto_redigido_abre_o_do_canal_externo_com_a_frase_fixa_sem_botao(
    sessao, conversa, politica
):
    chatwoot = ChatwootFalso()
    conversa.chatwoot_conversa_id = 555

    _handoff_com_texto(sessao, conversa, politica, chatwoot, TEXTO_REDIGIDO)

    assert chatwoot.enviadas(555) == [f"{TEXTO_REDIGIDO}\n\n{TEXTO_CANAL_EXTERNO}"]
    assert chatwoot.chamadas[-1][3] == []


def test_sem_texto_o_handoff_e_igual_ao_de_hoje(sessao, conversa, politica):
    chatwoot = ChatwootFalso()
    conversa.chatwoot_conversa_id = 555

    _handoff_com_texto(sessao, conversa, politica, chatwoot, None)

    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def _handoff_com_texto(sessao, conversa, politica, chatwoot, texto):
    fazer_handoff(
        sessao,
        chatwoot,
        conversa,
        MotivoHumano.PEDIU_HUMANO,
        [],
        politica,
        None,
        AGORA,
        texto=texto,
    )
