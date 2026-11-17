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
from agente.turno.catalogo import MOTIVOS_DE_FRASE_FIXA, TEXTO_CANAL_EXTERNO
from agente.turno.handoff import devolver_ao_robo, enviar_texto, fazer_handoff

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


# A passagem redigida pelo Luna sai no lugar da frase fixa

PASSAGEM = "Entendi, Ana. Um atendente da unidade vai continuar com você aqui, nesta conversa."


def _handoff_com_texto(sessao, conversa, politica, chatwoot, motivo, **opcoes):
    conversa.chatwoot_conversa_id = 555
    fazer_handoff(
        sessao, chatwoot, conversa, motivo, LAUDOS, politica, 4, AGORA, texto=PASSAGEM, **opcoes
    )


def test_enviar_texto_envia_e_grava_fora_do_teto_com_a_origem_dada(sessao, conversa, politica):
    conversa.chatwoot_conversa_id = 555
    chatwoot = ChatwootFalso()

    enviar_texto(sessao, chatwoot, conversa, PASSAGEM, OrigemMensagem.AGENTE, AGORA)

    assert chatwoot.enviadas(555) == [PASSAGEM]
    mensagem = sessao.scalars(select(Mensagem)).one()
    assert (mensagem.origem, mensagem.texto) == (OrigemMensagem.AGENTE, PASSAGEM)
    assert mensagem.conta_no_teto is False and mensagem.chatwoot_message_id == "falso-1"


def test_handoff_com_texto_manda_a_passagem_no_lugar_da_fixa_e_na_mesma_ordem(
    sessao, conversa, politica
):
    chatwoot = ChatwootFalso()

    _handoff_com_texto(sessao, conversa, politica, chatwoot, MotivoHumano.CONCILIACAO)

    assert [chamada[0] for chamada in chatwoot.chamadas] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
        "enviar_mensagem",
    ]
    assert chatwoot.enviadas(555) == [PASSAGEM]
    assert chatwoot.chamadas[-1][3] == []
    mensagem = sessao.scalars(select(Mensagem)).one()
    assert (mensagem.origem, mensagem.texto) == (OrigemMensagem.AGENTE, PASSAGEM)
    assert mensagens_no_teto(sessao, conversa.id) == 0
    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert conversa.motivo_humano is MotivoHumano.CONCILIACAO


def test_handoff_com_texto_e_origem_fixa_grava_como_fixa(sessao, conversa, politica):
    _handoff_com_texto(
        sessao,
        conversa,
        politica,
        ChatwootFalso(),
        MotivoHumano.CONCILIACAO,
        origem_do_texto=OrigemMensagem.FIXA,
    )

    assert sessao.scalars(select(Mensagem)).one().origem is OrigemMensagem.FIXA


def test_juridico_com_texto_recebe_a_fixa_do_canal_externo(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff_com_texto(sessao, conversa, politica, chatwoot, MotivoHumano.JURIDICO)

    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert sessao.scalars(select(Mensagem)).one().origem is OrigemMensagem.FIXA


def test_desconfianca_com_texto_recebe_a_fixa_do_whatsapp(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff_com_texto(sessao, conversa, politica, chatwoot, MotivoHumano.DESCONFIANCA)

    assert chatwoot.enviadas(555) == [politica.texto_humano_no_whatsapp]
    assert sessao.scalars(select(Mensagem)).one().origem is OrigemMensagem.FIXA


def test_sem_enviar_mensagem_o_texto_nao_sai(sessao, conversa, politica):
    chatwoot = ChatwootFalso()

    _handoff_com_texto(
        sessao, conversa, politica, chatwoot, MotivoHumano.CONCILIACAO, enviar_mensagem=False
    )

    assert chatwoot.enviadas(555) == []
    assert sessao.scalars(select(Mensagem)).all() == []
    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO


def test_texto_vazio_cai_na_fixa(sessao, conversa, politica):
    chatwoot = ChatwootFalso()
    conversa.chatwoot_conversa_id = 555

    fazer_handoff(
        sessao, chatwoot, conversa, MotivoHumano.CONCILIACAO, [], politica, 4, AGORA, texto=""
    )

    assert chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def test_so_o_juridico_e_o_golpe_tem_frase_fixa_obrigatoria():
    assert MOTIVOS_DE_FRASE_FIXA == {MotivoHumano.JURIDICO, MotivoHumano.DESCONFIANCA}
