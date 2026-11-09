"""As baixas externas (spec do CTN real, decisão 3): quem quitou por outro caminho para de ser
cobrado, quem pagou parte segue com o valor novo, e tudo idempotente pelo `id` da baixa."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import (
    BaixaCTNRegistro,
    Cliente,
    Cobranca,
    Conversa,
    CursorCTN,
    Mensagem,
    Pagamento,
    Transicao,
)
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import BaixaCTN, CTNFalso, CTNIndisponivel, MensalidadeAberta
from agente.pagamento import baixas_ctn as baixas_modulo
from agente.pagamento.baixas_ctn import (
    CURSOR_BAIXAS,
    MAX_CONFERENCIAS_POR_CICLO,
    DependenciasDasBaixas,
    ResumoBaixas,
    ler_baixas,
)
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    FormaHistorico,
    FormaPagamento,
    Modo,
    OrigemMensagem,
    StatusCobranca,
    StatusPagamento,
)
from agente.turno.liberacao import liberar_tudo

# 12h em Fortaleza, segunda-feira; o mês corrente é 2026/11.
AGORA = datetime(2026, 11, 9, 15, tzinfo=UTC)
MATRICULA = "M-0001"
NOME = "Cliente Fictício"
TELEFONE = "+5583900000001"


class ChatwootQueCai(ChatwootFalso):
    def __init__(self):
        super().__init__()
        self.fora = False

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        if self.fora:
            raise httpx.ConnectError("o Chatwoot não respondeu")
        return super().enviar_mensagem(conversa_id, texto, botoes)

    def mensagens_do_agente(self, conversa_id):
        if self.fora:
            raise httpx.ConnectError("o Chatwoot não respondeu")
        return super().mensagens_do_agente(conversa_id)


class CTNEspiao(CTNFalso):
    """Conta as leituras e deixa a conferência falhar sozinha, com o resto do CTN no ar."""

    def __init__(self, **resto):
        super().__init__(**resto)
        self.cursores = []
        self.vistos = []
        self.conferencias = 0
        self.conferencia_fora = False

    def baixas_desde(self, coletado_desde, vistos=()):
        self.cursores.append(coletado_desde)
        self.vistos.append(set(vistos))
        return super().baixas_desde(coletado_desde, vistos)

    def consultar_mensalidades(self, matricula):
        self.conferencias += 1
        if self.conferencia_fora:
            raise CTNIndisponivel("a conferência caiu (simulado)")
        return super().consultar_mensalidades(matricula)


def _baixa(id_externo="b-1", mes="2026/10", minutos=20, matricula=MATRICULA, valor=3340):
    return BaixaCTN(
        id_externo=id_externo,
        matricula=matricula,
        mes_ref=mes,
        valor_centavos=valor,
        forma=FormaHistorico.BOLETO,
        pago_em=AGORA - timedelta(hours=2),
        coletado_em=AGORA - timedelta(minutes=minutos),
    )


def _abertas(*meses):
    return [MensalidadeAberta(mes, 3340) for mes in meses]


def _ctn(baixas, abertas=(), rejeitadas=3, matricula=MATRICULA):
    """O CTN em que a conferência acha `abertas` (vazia com `rejeitadas` > 0 = quitou)."""
    return CTNEspiao(
        baixas_externas=baixas,
        mensalidades_abertas={matricula: list(abertas)},
        rejeitadas_lidas={matricula: rejeitadas},
        hoje=AGORA.date(),
    )


def _deps(politica, ctn, *, chatwoot=None, modo=Modo.ATIVO, liberar=True):
    return DependenciasDasBaixas(
        politica=liberar_tudo(politica) if liberar else politica,
        chatwoot=chatwoot or ChatwootFalso(),
        time_humano=4,
        modo=modo,
        ctn=ctn,
    )


@pytest.fixture
def ligada(sessao, conversa):
    """A conversa de uma cobrança com 3 meses em aberto, em que o cliente escreveu há uma hora."""
    conversa.chatwoot_conversa_id = 555
    conversa.estado = EstadoConversa.AGUARDANDO_PAGAMENTO
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.CLIENTE, "oi", "cw-1", False, AGORA - timedelta(hours=1)
    )
    sessao.commit()
    return conversa


def _registro(sessao, id_externo="b-1"):
    return sessao.scalars(
        select(BaixaCTNRegistro).where(BaixaCTNRegistro.id_externo == id_externo)
    ).one()


def _fixas(sessao):
    return list(sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA)))


# ---- quitou ----


def test_quitou_vira_paga_encerra_a_conversa_e_manda_o_aviso_uma_vez(sessao, ligada, politica):
    ctn = _ctn([_baixa()])
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo == ResumoBaixas(lidas=1, aplicadas=1, quitadas=1, abatidas=0, ignoradas=0)
    cobranca = ligada.cobranca
    assert cobranca.status is StatusCobranca.PAGA
    assert cobranca.encerrada_motivo == "baixa_externa"
    assert cobranca.encerrada_em == AGORA
    assert cobranca.meses_em_aberto == []
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert [t.para for t in sessao.scalars(select(Transicao).order_by(Transicao.id))] == [
        EstadoConversa.PAGO,
        EstadoConversa.ENCERRADO,
    ]
    assert deps.chatwoot.enviadas(555) == [politica.texto_baixa_externa]
    [aviso] = _fixas(sessao)
    assert aviso.texto == politica.texto_baixa_externa
    assert aviso.conta_no_teto is False
    assert aviso.envio is EstadoEnvio.ENVIADA
    assert aviso.chatwoot_message_id
    registro = _registro(sessao)
    assert registro.efeito == "quitou"
    assert registro.cobranca_id == cobranca.id
    assert registro.aplicada_em == AGORA


def test_o_aviso_nao_leva_valor_nem_nome(sessao, ligada, politica):
    deps = _deps(politica, _ctn([_baixa()]))
    ler_baixas(sessao, deps, AGORA)
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$" not in texto
    assert "Cliente" not in texto


def test_varias_baixas_da_mesma_matricula_quitam_uma_vez_e_avisam_uma_vez(sessao, ligada, politica):
    ctn = _ctn(
        [_baixa("b-1", "2026/08", 30), _baixa("b-2", "2026/09", 25), _baixa("b-3", "2026/10")]
    )
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo.lidas == 3
    assert resumo.aplicadas == 3
    assert resumo.quitadas == 1
    assert ctn.conferencias == 1
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _registro(sessao, "b-1").efeito == "quitou"
    assert _registro(sessao, "b-2").efeito == "conferida_junto"
    assert _registro(sessao, "b-3").efeito == "conferida_junto"


# ---- pagou parte ----


def test_pagou_parte_atualiza_meses_divida_e_regua_sem_mensagem(sessao, ligada, politica):
    ctn = _ctn([_baixa()], abertas=_abertas("2026/08", "2026/09"))
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo == ResumoBaixas(lidas=1, aplicadas=1, quitadas=0, abatidas=1, ignoradas=0)
    cobranca = ligada.cobranca
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.meses_em_aberto == ["2026/08", "2026/09"]
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.divida_centavos == 6680
    assert cobranca.regua is Regua.R2
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert deps.chatwoot.enviadas(555) == []
    assert _fixas(sessao) == []
    assert _registro(sessao).efeito == "abateu"


def test_baixa_que_nao_muda_os_meses_em_aberto_nao_abate(sessao, ligada, politica):
    ctn = _ctn([_baixa()], abertas=_abertas("2026/07", "2026/08", "2026/09"))
    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)
    assert resumo == ResumoBaixas(lidas=1, aplicadas=1, quitadas=0, abatidas=0, ignoradas=1)
    assert _registro(sessao).efeito == "sem_mudanca"
    assert ligada.cobranca.mensalidades_abertas == 3


# ---- mês corrente ----


def test_baixa_do_mes_corrente_ainda_a_vencer_e_ignorada_sem_conferir(sessao, ligada, politica):
    # O mês corrente não está entre os em aberto: o cliente pagou adiantado, não há o que abater.
    ligada.cobranca.meses_em_aberto = ["2026/08", "2026/09"]
    sessao.commit()
    ctn = _ctn([_baixa(mes="2026/11")])
    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)
    assert resumo == ResumoBaixas(lidas=1, aplicadas=1, quitadas=0, abatidas=0, ignoradas=1)
    assert ctn.conferencias == 0
    assert ligada.cobranca.status is StatusCobranca.ATIVA
    assert ligada.cobranca.mensalidades_abertas == 3
    assert _registro(sessao).efeito == "ignorada"
    assert _registro(sessao).cobranca_id == ligada.cobranca.id


def test_baixa_do_mes_corrente_em_atraso_e_conferida_e_quita(sessao, ligada, politica):
    # O cliente R1 venceu dia 5, o cartão de 2026/11 foi recusado e ele pagou no balcão dia 20.
    ligada.cobranca.meses_em_aberto = ["2026/11"]
    ligada.cobranca.mensalidades_abertas = 1
    sessao.commit()
    ctn = _ctn([_baixa(mes="2026/11")])
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert ctn.conferencias == 1
    assert resumo.quitadas == 1
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert deps.chatwoot.enviadas(555) == [politica.texto_baixa_externa]
    assert _registro(sessao).efeito == "quitou"


def test_baixa_do_mes_corrente_sem_meses_em_aberto_gravados_e_conferida(sessao, ligada, politica):
    assert ligada.cobranca.meses_em_aberto is None
    ctn = _ctn([_baixa(mes="2026/11")])

    ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert ctn.conferencias == 1
    assert ligada.cobranca.status is StatusCobranca.PAGA


def test_o_mes_corrente_e_o_de_fortaleza_e_nao_o_do_utc(sessao, ligada, politica):
    # 31/10 às 22h em Fortaleza já é 1º de novembro no UTC: outubro ainda é o mês corrente.
    ligada.cobranca.meses_em_aberto = ["2026/08", "2026/09"]
    sessao.commit()
    agora = datetime(2026, 11, 1, 1, tzinfo=UTC)
    ctn = _ctn([replace(_baixa(mes="2026/10"), coletado_em=agora - timedelta(minutes=5))])
    ler_baixas(sessao, _deps(politica, ctn), agora)
    assert ctn.conferencias == 0
    assert _registro(sessao).efeito == "ignorada"


# ---- cobrança que não é ativa ----


def test_cobranca_ja_paga_registra_e_ignora_sem_mensagem(sessao, ligada, politica):
    ligada.cobranca.status = StatusCobranca.PAGA
    sessao.commit()
    ctn = _ctn([_baixa()])
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo == ResumoBaixas(lidas=1, aplicadas=1, quitadas=0, abatidas=0, ignoradas=1)
    assert ctn.conferencias == 0
    assert deps.chatwoot.chamadas == []
    registro = _registro(sessao)
    assert registro.efeito == "ja_paga"
    assert registro.cobranca_id == ligada.cobranca.id


def test_sem_cobranca_ativa_grava_a_baixa_e_segue(sessao, ligada, politica):
    ligada.cobranca.status = StatusCobranca.ENCERRADA
    sessao.commit()
    ctn = _ctn([_baixa("b-1"), _baixa("b-2", matricula="M-DESCONHECIDA")])

    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert resumo == ResumoBaixas(lidas=2, aplicadas=2, quitadas=0, abatidas=0, ignoradas=2)
    assert ctn.conferencias == 0
    assert _registro(sessao, "b-1").efeito == "sem_cobranca"
    assert _registro(sessao, "b-1").cobranca_id is None
    assert _registro(sessao, "b-2").efeito == "sem_cobranca"
    assert ligada.cobranca.status is StatusCobranca.ENCERRADA


# ---- idempotência e cursor ----


def test_baixa_repetida_nao_duplica_nem_reaplica(sessao, ligada, politica):
    ctn = _ctn([_baixa()])
    deps = _deps(politica, ctn)
    ler_baixas(sessao, deps, AGORA)

    segunda = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert segunda == ResumoBaixas()
    assert len(sessao.scalars(select(BaixaCTNRegistro)).all()) == 1
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert len(_fixas(sessao)) == 1


def test_o_cursor_guarda_a_coleta_mais_nova_e_so_anda_depois_de_gravar(sessao, ligada, politica):
    ctn = _ctn([_baixa("b-1", minutos=30), _baixa("b-2", minutos=20)])
    ler_baixas(sessao, _deps(politica, ctn), AGORA)
    assert sessao.get(CursorCTN, CURSOR_BAIXAS).valor == (AGORA - timedelta(minutes=20)).isoformat()

    ler_baixas(sessao, _deps(politica, ctn), AGORA + timedelta(minutes=10))
    # A leitura seguinte parte do cursor (inclusivo), não de "agora".
    assert ctn.cursores[-1] == AGORA - timedelta(minutes=20)


def test_a_primeira_leitura_comeca_tres_horas_antes(sessao, ligada, politica):
    ctn = _ctn([])
    ler_baixas(sessao, _deps(politica, ctn), AGORA)
    assert ctn.cursores == [AGORA - timedelta(hours=3)]


def test_ctn_fora_nao_anda_o_cursor_nem_levanta(sessao, ligada, politica, caplog):
    ctn = _ctn([_baixa("b-1", minutos=30)])
    deps = _deps(politica, ctn)
    ler_baixas(sessao, deps, AGORA)
    cursor = sessao.get(CursorCTN, CURSOR_BAIXAS).valor
    ctn.baixas_externas.append(_baixa("b-2", minutos=5))
    ctn.fora_do_ar = True

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert resumo == ResumoBaixas()
    assert sessao.get(CursorCTN, CURSOR_BAIXAS).valor == cursor
    assert len(sessao.scalars(select(BaixaCTNRegistro)).all()) == 1

    ctn.fora_do_ar = False
    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))
    assert resumo.lidas == 1  # a b-2 não se perdeu


# ---- a janela de 24h, a sombra e o humano ----


def test_fora_da_janela_de_24h_nao_envia_e_o_efeito_diz(sessao, ligada, politica):
    mensagem = sessao.scalars(
        select(Mensagem).where(Mensagem.origem == OrigemMensagem.CLIENTE)
    ).one()
    mensagem.momento = AGORA - timedelta(hours=30)
    sessao.commit()
    deps = _deps(politica, _ctn([_baixa()]))

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo.quitadas == 1
    assert deps.chatwoot.chamadas == []
    assert _fixas(sessao) == []
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert ligada.estado is EstadoConversa.ENCERRADO
    assert _registro(sessao).efeito == "quitou_fora_da_janela"


def test_quitou_sem_conversa_so_encerra_a_cobranca(sessao, politica):
    cliente = Cliente(matricula_ctn=MATRICULA, nome=NOME, telefone=TELEFONE)
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha="nov",
    )
    sessao.add(cobranca)
    sessao.commit()
    deps = _deps(politica, _ctn([_baixa()]))

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo.quitadas == 1
    assert cobranca.status is StatusCobranca.PAGA
    assert deps.chatwoot.chamadas == []
    assert _registro(sessao).efeito == "quitou_sem_conversa"


def test_em_sombra_a_cobranca_quita_mas_nada_sai_e_a_conversa_nao_muda(sessao, ligada, politica):
    deps = _deps(politica, _ctn([_baixa()]), modo=Modo.SOMBRA)

    ler_baixas(sessao, deps, AGORA)

    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert deps.chatwoot.chamadas == []
    assert _registro(sessao).efeito == "quitou_sombra"


def test_cliente_fora_do_piloto_nao_recebe_o_aviso(sessao, ligada, politica):
    deps = _deps(politica, _ctn([_baixa()]), liberar=False)  # a política padrão é toda em sombra

    ler_baixas(sessao, deps, AGORA)

    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert deps.chatwoot.chamadas == []
    assert _registro(sessao).efeito == "quitou_sombra"


def test_com_atendente_na_conversa_o_cliente_nao_recebe_o_aviso_e_o_atendente_ganha_a_nota(
    sessao, ligada, politica
):
    ligada.human_owned = True
    sessao.commit()
    deps = _deps(politica, _ctn([_baixa()]))

    ler_baixas(sessao, deps, AGORA)

    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert deps.chatwoot.enviadas(555) == []
    notas = [c for c in deps.chatwoot.chamadas if c[0] == "nota_privada"]
    assert len(notas) == 1 and notas[0][1] == 555
    assert _registro(sessao).efeito == "quitou_com_humano"


# ---- o Chatwoot cai ----


def test_chatwoot_fora_deixa_o_aviso_pendente_e_o_proximo_ciclo_envia_uma_vez(
    sessao, ligada, politica
):
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo.quitadas == 1
    assert ligada.cobranca.status is StatusCobranca.PAGA
    [aviso] = _fixas(sessao)
    assert aviso.envio is EstadoEnvio.PENDENTE
    assert _registro(sessao).efeito == "quitou_aviso_pendente"

    chatwoot.fora = False
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))

    assert chatwoot.enviadas(555) == [politica.texto_baixa_externa]
    assert aviso.envio is EstadoEnvio.ENVIADA
    assert _registro(sessao).efeito == "quitou"


def test_se_a_janela_fechar_antes_do_reenvio_o_aviso_e_descartado(sessao, ligada, politica):
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)
    ler_baixas(sessao, deps, AGORA)

    chatwoot.fora = False
    ler_baixas(sessao, deps, AGORA + timedelta(hours=25))

    assert chatwoot.enviadas(555) == []
    [aviso] = _fixas(sessao)
    assert aviso.envio is EstadoEnvio.DESCARTADA
    assert _registro(sessao).efeito == "quitou_fora_da_janela"


# ---- a conferência não rodou ----


def test_conferencia_fora_deixa_a_baixa_pendente_e_o_proximo_ciclo_aplica(sessao, ligada, politica):
    ctn = _ctn([_baixa()])
    ctn.conferencia_fora = True
    deps = _deps(politica, ctn)

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo == ResumoBaixas(lidas=1)
    assert ligada.cobranca.status is StatusCobranca.ATIVA
    assert _registro(sessao).aplicada_em is None

    ctn.conferencia_fora = False
    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert resumo == ResumoBaixas(lidas=0, aplicadas=1, quitadas=1)
    assert ligada.cobranca.status is StatusCobranca.PAGA
    assert _registro(sessao).efeito == "quitou"
    assert len(deps.chatwoot.enviadas(555)) == 1


def test_a_baixa_que_nao_se_confere_em_24h_e_encerrada_sem_efeito(sessao, ligada, politica):
    ctn = _ctn([_baixa()])
    ctn.conferencia_fora = True
    deps = _deps(politica, ctn)
    ler_baixas(sessao, deps, AGORA)

    resumo = ler_baixas(sessao, deps, AGORA + timedelta(hours=25))

    assert resumo.aplicadas == 1 and resumo.quitadas == 0
    assert _registro(sessao).efeito == "nao_conferida"
    assert _registro(sessao).aplicada_em is not None
    assert ligada.cobranca.status is StatusCobranca.ATIVA


@pytest.mark.parametrize(
    ("abertas", "rejeitadas"),
    [
        pytest.param((), 0, id="sem_lancamentos"),
        pytest.param(
            _abertas("2026/01", "2026/02", "2026/03", "2026/04", "2026/05"), 3, id="suspeito"
        ),
    ],
)
def test_conferencia_que_nao_muda_numa_nova_tentativa_nao_e_repetida(
    sessao, ligada, politica, abertas, rejeitadas
):
    ctn = _ctn([_baixa()], abertas=abertas, rejeitadas=rejeitadas)
    deps = _deps(politica, ctn)

    ler_baixas(sessao, deps, AGORA)

    # Sem lançamentos ou com dado suspeito, a resposta de hoje é a de amanhã: encerra na hora.
    assert ctn.conferencias == 1
    assert _registro(sessao).efeito == "nao_conferida"
    assert _registro(sessao).aplicada_em == AGORA
    assert ligada.cobranca.status is StatusCobranca.ATIVA

    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))
    assert ctn.conferencias == 1


# ---- sem PII no log ----


def test_o_log_nao_leva_matricula_nome_nem_telefone(sessao, ligada, politica, caplog):
    ctn = _ctn([_baixa("b-1", minutos=40), _baixa("b-2", matricula="M-OUTRA")])
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    deps = _deps(politica, ctn, chatwoot=chatwoot)

    with caplog.at_level("DEBUG"):
        ler_baixas(sessao, deps, AGORA)
        ctn.fora_do_ar = True
        ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))
        ctn.fora_do_ar = False
        ctn.conferencia_fora = True
        ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))

    for dado in (MATRICULA, "M-OUTRA", NOME, TELEFONE, "900000001"):
        assert dado not in caplog.text


# ---- o cursor com vários ids no mesmo instante, a coleta no futuro e a margem ----


def test_a_leitura_seguinte_leva_os_ids_ja_gravados_do_instante_do_cursor(sessao, ligada, politica):
    ctn = _ctn(
        [_baixa("b-1", minutos=30), _baixa("b-2", minutos=20), _baixa("b-3", minutos=20)],
        abertas=_abertas("2026/08", "2026/09"),
    )
    deps = _deps(politica, ctn)
    ler_baixas(sessao, deps, AGORA)
    assert ctn.vistos[0] == set()

    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert ctn.cursores[-1] == AGORA - timedelta(minutes=20)
    # Os do instante do cursor, e só eles (a b-1 é de antes). Não voltam, então nada é "lido".
    assert ctn.vistos[-1] == {"b-2", "b-3"}
    assert resumo.lidas == 0


def test_baixa_com_coletado_em_no_futuro_nao_leva_o_cursor_para_o_futuro(sessao, ligada, politica):
    # O relógio do coletor errado: a baixa vem datada de 3 dias à frente.
    futura = replace(_baixa("b-futura"), coletado_em=AGORA + timedelta(days=3))
    ctn = _ctn([_baixa("b-1", minutos=30), futura], abertas=_abertas("2026/08", "2026/09"))
    deps = _deps(politica, ctn)

    ler_baixas(sessao, deps, AGORA)

    assert datetime.fromisoformat(sessao.get(CursorCTN, CURSOR_BAIXAS).valor) == AGORA
    # A baixa que chega depois, coletada no tempo certo, não se perde atrás de um cursor no futuro.
    ctn.baixas_externas.append(
        replace(_baixa("b-nova", "2026/09"), coletado_em=AGORA + timedelta(minutes=5))
    )
    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))
    assert resumo.lidas == 1
    assert _registro(sessao, "b-nova")


def test_margem_de_releitura_recua_o_cursor_e_os_ja_gravados_nao_se_repetem(
    sessao, ligada, politica, monkeypatch
):
    monkeypatch.setattr("agente.pagamento.baixas_ctn.MARGEM_DE_RELEITURA", timedelta(minutes=45))
    ctn = _ctn(
        [_baixa("b-1", minutos=30), _baixa("b-2", minutos=20)],
        abertas=_abertas("2026/08", "2026/09"),
    )
    deps = _deps(politica, ctn)
    ler_baixas(sessao, deps, AGORA)

    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    # O cursor guardado é o do b-2; o pedido recua 45 min, e as duas já gravadas vão em `vistos`.
    assert ctn.cursores[-1] == AGORA - timedelta(minutes=20) - timedelta(minutes=45)
    assert ctn.vistos[-1] == {"b-1", "b-2"}
    assert resumo.lidas == 0
    assert len(sessao.scalars(select(BaixaCTNRegistro)).all()) == 2


def test_a_margem_comeca_em_zero():
    from agente.pagamento.baixas_ctn import MARGEM_DE_RELEITURA

    assert MARGEM_DE_RELEITURA == timedelta(0)


# ---- a linha malformada da API e a conferência com o cache velho (CTNEficaz de verdade) ----


def _tabela_eficaz(linhas_bx, lancamentos=()):
    return {"/v1/ctn/lancamentos_bx": linhas_bx, "/v1/ctn/lancamentos": list(lancamentos)}


def _bx_eficaz(id_, matricula, minutos, dia="2026-11-09"):
    from tests.integracoes.test_ctn_eficaz import _bx

    coletado = (AGORA - timedelta(minutes=minutos)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return _bx(id_, matricula, "PIX", "2026/10", dia, 33.4, coletado)


def test_linha_malformada_do_ctn_nao_trava_o_cursor_e_as_boas_sao_gravadas(
    sessao, ligada, politica, caplog
):
    from tests.integracoes.test_ctn_eficaz import cliente as cliente_eficaz
    from tests.integracoes.test_ctn_eficaz import servidor

    ruim = _bx_eficaz(62, "xx000000062", 25)
    ruim["dt_baixa"] = None
    tabelas = _tabela_eficaz(
        [_bx_eficaz(61, "xx000000061", 30), ruim, _bx_eficaz(63, "xx000000063", 20)]
    )
    ctn = cliente_eficaz(servidor(tabelas), agora=lambda: AGORA)
    deps = _deps(politica, ctn)

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, deps, AGORA)
        segunda = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert resumo.lidas == 2
    assert segunda.lidas == 0
    assert {r.id_externo for r in sessao.scalars(select(BaixaCTNRegistro))} == {"61", "63"}
    # O cursor andou até a última boa, em vez de falhar a cada ciclo.
    assert sessao.get(CursorCTN, CURSOR_BAIXAS).valor == (AGORA - timedelta(minutes=20)).isoformat()
    registro = " ".join(r.getMessage() for r in caplog.records)
    assert "xx0000" not in registro and "Nome Inventado" not in registro
    assert "falhou" not in registro


def test_a_conferencia_da_rotina_nao_usa_o_cache_velho_das_baixas_da_pessoa(
    sessao, ligada, politica
):
    from tests.integracoes.test_ctn_eficaz import _lanc, servidor
    from tests.integracoes.test_ctn_eficaz import cliente as cliente_eficaz

    lancamentos = [
        _lanc(1, "2026/08", "rejeitada", data_baixa="2026-08-12", matricula=MATRICULA),
        _lanc(2, "2026/09", "rejeitada", data_baixa="2026-09-12", matricula=MATRICULA),
        _lanc(3, "2026/10", "rejeitada", data_baixa="2026-10-12", matricula=MATRICULA),
    ]
    tabelas = _tabela_eficaz([], lancamentos)
    relogio = [1000.0]
    ctn = cliente_eficaz(servidor(tabelas), agora=lambda: AGORA, relogio=lambda: relogio[0])
    # Um turno leu as baixas do cliente há 10 s (nenhuma ainda) e o cache guardou a lista vazia.
    assert ctn.historico_pagamentos(MATRICULA, 12) == []
    relogio[0] += 10
    # A baixa de 2026/10 aparece no CTN: a conferência da rotina tem de enxergá-la.
    tabelas["/v1/ctn/lancamentos_bx"].append(_bx_eficaz(70, MATRICULA, 5))

    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert resumo.abatidas == 1
    assert _registro(sessao, "70").efeito == "abateu"


def test_60_baixas_no_mesmo_instante_custam_uma_chamada_por_ciclo_depois_do_primeiro(
    sessao, politica
):
    from tests.integracoes.test_ctn_eficaz import cliente as cliente_eficaz
    from tests.integracoes.test_ctn_eficaz import servidor

    antigas = [_bx_eficaz(1000 + n, f"xx{n:09d}", 240 + 60 * n) for n in range(30)]
    do_instante = [_bx_eficaz(2000 + n, f"yy{n:09d}", 20) for n in range(60)]
    chamadas = []
    ctn = cliente_eficaz(
        servidor(_tabela_eficaz(antigas + do_instante), chamadas), agora=lambda: AGORA
    )
    deps = _deps(politica, ctn)

    custos = []
    for ciclo in range(4):
        antes = len(chamadas)
        resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10 * ciclo))
        custos.append((len(chamadas) - antes, resumo.lidas))

    # Ciclo 0: as 60 são novas (página toda nova): 1 + a janela por `id` (90 linhas, 2 páginas).
    # Ciclos 1 a 3: nada novo; as 60 do instante do cursor já estão gravadas: 1 chamada.
    assert custos == [(3, 60), (1, 0), (1, 0), (1, 0)]


# ---- várias cobranças: o teto por ciclo e o CTN fora ----


def _nova_cobranca(sessao, matricula, numero, *, mensagem_ha=timedelta(hours=1)):
    """Uma cobrança ATIVA de 3 meses, com a conversa ligada ao Chatwoot (id `1000 + numero`)."""
    cliente = Cliente(
        matricula_ctn=matricula, nome="Fulano Fictício", telefone=f"+558390{numero:07d}"
    )
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha="nov",
    )
    conversa = Conversa(
        cobranca=cobranca,
        criada_em=AGORA - timedelta(days=2),
        chatwoot_conversa_id=1000 + numero,
        estado=EstadoConversa.AGUARDANDO_PAGAMENTO,
    )
    sessao.add(conversa)
    sessao.flush()
    registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.CLIENTE,
        "oi",
        f"cw-{numero}",
        False,
        AGORA - mensagem_ha,
    )
    sessao.commit()
    return cobranca, conversa


def _ctn_de_varias(matriculas, rejeitadas=3):
    baixas = [
        _baixa(f"b-{n}", minutos=60 - n, matricula=matricula)
        for n, matricula in enumerate(matriculas)
    ]
    return CTNEspiao(
        baixas_externas=baixas,
        mensalidades_abertas={matricula: [] for matricula in matriculas},
        rejeitadas_lidas={matricula: rejeitadas for matricula in matriculas},
        hoje=AGORA.date(),
    )


def _por_tratar(sessao):
    return sessao.scalars(
        select(BaixaCTNRegistro).where(BaixaCTNRegistro.aplicada_em.is_(None))
    ).all()


def test_a_rotina_confere_no_maximo_dez_matriculas_por_ciclo_e_o_resto_fica_para_o_proximo(
    sessao, politica
):
    assert MAX_CONFERENCIAS_POR_CICLO == 10
    matriculas = [f"M-{n:04d}" for n in range(15)]
    for numero, matricula in enumerate(matriculas):
        _nova_cobranca(sessao, matricula, numero)
    ctn = _ctn_de_varias(matriculas)
    deps = _deps(politica, ctn)

    primeiro = ler_baixas(sessao, deps, AGORA)

    assert ctn.conferencias == 10
    assert primeiro.lidas == 15 and primeiro.quitadas == 10
    assert len(_por_tratar(sessao)) == 5

    segundo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert ctn.conferencias == 15
    assert segundo.lidas == 0 and segundo.quitadas == 5
    assert _por_tratar(sessao) == []


def test_baixa_sem_cobranca_nao_gasta_o_teto_de_conferencias(sessao, politica):
    # A maioria das baixas do CTN é de quem nem está na carteira: só custam uma leitura no banco.
    desconhecidas = [f"X-{n:04d}" for n in range(25)]
    _nova_cobranca(sessao, "M-0001", 1)
    ctn = _ctn_de_varias([*desconhecidas, "M-0001"])

    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert resumo.lidas == 26 and resumo.aplicadas == 26
    assert ctn.conferencias == 1


def test_no_primeiro_ctn_fora_a_rotina_sai_do_laco(sessao, politica):
    matriculas = [f"M-{n:04d}" for n in range(5)]
    for numero, matricula in enumerate(matriculas):
        _nova_cobranca(sessao, matricula, numero)
    ctn = _ctn_de_varias(matriculas)
    ctn.conferencia_fora = True

    resumo = ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert ctn.conferencias == 1  # as outras quatro nem tentaram
    assert resumo == ResumoBaixas(lidas=5)
    assert len(_por_tratar(sessao)) == 5


def test_cobranca_fechada_durante_a_conferencia_nao_e_ctn_fora(sessao, politica):
    matriculas = ["M-0000", "M-0001"]
    primeira, _ = _nova_cobranca(sessao, "M-0000", 0)
    segunda, _ = _nova_cobranca(sessao, "M-0001", 1)
    ctn = _ctn_de_varias(matriculas)
    consultar = ctn.consultar_mensalidades

    def fechando(matricula):
        resposta = consultar(matricula)
        if matricula == "M-0000":
            primeira.status = StatusCobranca.ENCERRADA
            sessao.commit()
        return resposta

    ctn.consultar_mensalidades = fechando

    ler_baixas(sessao, _deps(politica, ctn), AGORA)

    # A cobrança fechou no meio (não foi o CTN): a rotina seguiu para a outra matrícula.
    assert segunda.status is StatusCobranca.PAGA


# ---- o reenvio do aviso confere de novo o modo, o piloto e o atendente ----


def _com_aviso_pendente(sessao, politica):
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)
    ler_baixas(sessao, deps, AGORA)
    assert _registro(sessao).efeito == "quitou_aviso_pendente"
    chatwoot.fora = False
    return deps, chatwoot


def test_reenvio_com_o_servico_de_volta_a_sombra_descarta_o_aviso(sessao, ligada, politica):
    deps, chatwoot = _com_aviso_pendente(sessao, politica)

    ler_baixas(sessao, replace(deps, modo=Modo.SOMBRA), AGORA + timedelta(minutes=10))

    assert chatwoot.enviadas(555) == []
    [aviso] = _fixas(sessao)
    assert aviso.envio is EstadoEnvio.DESCARTADA
    assert _registro(sessao).efeito == "quitou_sombra"


def test_reenvio_com_o_telefone_fora_do_piloto_descarta_o_aviso(sessao, ligada, politica):
    deps, chatwoot = _com_aviso_pendente(sessao, politica)
    fora_do_piloto = replace(deps, politica=politica)  # a política padrão é toda em sombra

    ler_baixas(sessao, fora_do_piloto, AGORA + timedelta(minutes=10))

    assert chatwoot.enviadas(555) == []
    [aviso] = _fixas(sessao)
    assert aviso.envio is EstadoEnvio.DESCARTADA
    assert _registro(sessao).efeito == "quitou_sombra"


def test_reenvio_com_atendente_que_assumiu_descarta_o_aviso_e_o_atendente_ganha_a_nota(
    sessao, ligada, politica
):
    deps, chatwoot = _com_aviso_pendente(sessao, politica)
    ligada.human_owned = True
    sessao.commit()

    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert chatwoot.enviadas(555) == []
    [aviso] = _fixas(sessao)
    assert aviso.envio is EstadoEnvio.DESCARTADA
    assert _registro(sessao).efeito == "quitou_com_humano_nota_pendente"

    ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))

    assert chatwoot.enviadas(555) == []
    assert [c[0] for c in chatwoot.chamadas].count("nota_privada") == 1
    assert _registro(sessao).efeito == "quitou_com_humano"


def test_uma_excecao_num_aviso_nao_derruba_a_passada_nem_impede_a_leitura(
    sessao, politica, monkeypatch, caplog
):
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    _nova_cobranca(sessao, MATRICULA, 1)
    _nova_cobranca(sessao, "M-0002", 2)
    ctn = _ctn_de_varias([MATRICULA, "M-0002"])
    deps = _deps(politica, ctn, chatwoot=chatwoot)
    ler_baixas(sessao, deps, AGORA)
    chatwoot.fora = False
    ctn.baixas_externas.append(_baixa("b-nova", "2026/09", 1, matricula="M-OUTRA"))
    leituras = len(ctn.cursores)

    original = baixas_modulo.dentro_das_24h
    chamadas = []

    def quebra_na_primeira(*args, **kwargs):
        chamadas.append(1)
        if len(chamadas) == 1:
            raise RuntimeError(f"INSERT ... [parameters: ('{MATRICULA}', '{NOME}')]")
        return original(*args, **kwargs)

    monkeypatch.setattr(baixas_modulo, "dentro_das_24h", quebra_na_primeira)

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert len(ctn.cursores) == leituras + 1  # o CTN foi lido mesmo assim
    assert resumo.lidas == 1
    efeitos = sorted(r.efeito for r in sessao.scalars(select(BaixaCTNRegistro)) if r.efeito)
    assert efeitos.count("quitou") == 1 and efeitos.count("quitou_aviso_pendente") == 1
    assert MATRICULA not in caplog.text and NOME not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


# ---- a nota ao atendente sai depois do commit, e as travas seguem a ordem do pagamento ----


class ChatwootVigiado(ChatwootQueCai):
    """Anota se havia escrita sem commit na transação quando o Chatwoot foi chamado."""

    def __init__(self, motor):
        super().__init__()
        self.escrevendo = False
        self.com_escrita_aberta = []
        self.nota_fora = False

        @event.listens_for(motor, "before_cursor_execute")
        def _escrita(_conn, _cursor, statement, *_resto):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                self.escrevendo = True

        @event.listens_for(motor, "commit")
        def _commit(_conn):
            self.escrevendo = False

        @event.listens_for(motor, "rollback")
        def _rollback(_conn):
            self.escrevendo = False

    def _vigiar(self, metodo):
        if self.escrevendo:
            self.com_escrita_aberta.append(metodo)

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        self._vigiar("enviar_mensagem")
        return super().enviar_mensagem(conversa_id, texto, botoes)

    def mensagens_do_agente(self, conversa_id):
        self._vigiar("mensagens_do_agente")
        return super().mensagens_do_agente(conversa_id)

    def nota_privada(self, conversa_id, texto):
        self._vigiar("nota_privada")
        if self.nota_fora:
            self.chamadas.append(("nota_privada", conversa_id, texto))
            raise httpx.ConnectError("o Chatwoot não respondeu")
        super().nota_privada(conversa_id, texto)


def test_nenhuma_chamada_ao_chatwoot_com_escrita_sem_commit(sessao, ligada, politica):
    ligada.human_owned = True
    sessao.commit()
    chatwoot = ChatwootVigiado(sessao.get_bind())
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)

    ler_baixas(sessao, deps, AGORA)

    assert any(c[0] == "nota_privada" for c in chatwoot.chamadas)
    assert chatwoot.com_escrita_aberta == []


def test_nenhuma_chamada_ao_chatwoot_com_escrita_sem_commit_no_aviso_e_no_reenvio(
    sessao, ligada, politica
):
    chatwoot = ChatwootVigiado(sessao.get_bind())
    chatwoot.fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)
    ler_baixas(sessao, deps, AGORA)
    chatwoot.fora = False

    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert chatwoot.enviadas(555)
    assert chatwoot.com_escrita_aberta == []


def test_a_nota_que_falhou_e_refeita_no_ciclo_seguinte(sessao, ligada, politica):
    ligada.human_owned = True
    sessao.commit()
    chatwoot = ChatwootVigiado(sessao.get_bind())
    chatwoot.nota_fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)

    ler_baixas(sessao, deps, AGORA)

    assert ligada.cobranca.status is StatusCobranca.PAGA  # a quitação não depende da nota
    assert _registro(sessao).efeito == "quitou_com_humano_nota_pendente"

    chatwoot.nota_fora = False
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=20))

    assert [c[0] for c in chatwoot.chamadas].count("nota_privada") == 2  # 1 que falhou + 1 que saiu
    assert _registro(sessao).efeito == "quitou_com_humano"
    assert chatwoot.com_escrita_aberta == []


def test_a_nota_que_nao_sai_em_24h_e_desistida(sessao, ligada, politica):
    ligada.human_owned = True
    sessao.commit()
    chatwoot = ChatwootVigiado(sessao.get_bind())
    chatwoot.nota_fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)
    ler_baixas(sessao, deps, AGORA)

    ler_baixas(sessao, deps, AGORA + timedelta(hours=25))

    assert _registro(sessao).efeito == "quitou_com_humano_sem_nota"


def _pedidos_de_trava(sessao):
    """Os `SELECT ... FOR UPDATE` emitidos, na ordem: (entidade, skip_locked)."""
    pedidos = []

    @event.listens_for(sessao.get_bind(), "before_execute")
    def _trava(_conn, clausula, *_resto):
        arg = getattr(clausula, "_for_update_arg", None)
        if arg is not None:
            entidade = clausula.column_descriptions[0]["entity"].__name__
            pedidos.append((entidade, arg.skip_locked))

    return pedidos


def _pix_pendente(cobranca, referencia, valor, forma=FormaPagamento.PIX):
    pagamento = Pagamento(
        forma=forma,
        valor_centavos=valor,
        referencia=referencia,
        status=StatusPagamento.PENDENTE,
        criado_em=AGORA - timedelta(minutes=30),
        expira_em=AGORA + timedelta(hours=20),
        br_code="000201..." if forma is FormaPagamento.PIX else None,
    )
    cobranca.pagamentos.append(pagamento)
    return pagamento


@pytest.mark.parametrize("abertas", [(), ("2026/08", "2026/09")], ids=["quitou", "abateu"])
def test_as_travas_seguem_a_ordem_conversa_cobranca_pagamento(sessao, ligada, politica, abertas):
    _pix_pendente(ligada.cobranca, "tx-antigo", 10020)
    sessao.commit()
    pedidos = _pedidos_de_trava(sessao)
    ctn = _ctn([_baixa()], abertas=_abertas(*abertas))

    ler_baixas(sessao, _deps(politica, ctn), AGORA)

    ordem = {"Conversa": 0, "Cobranca": 1, "Pagamento": 2}
    entidades = [entidade for entidade, _ in pedidos if entidade in ordem]
    assert entidades[:2] == ["Conversa", "Cobranca"]
    assert [ordem[e] for e in entidades] == sorted(ordem[e] for e in entidades)
    if abertas:
        assert "Pagamento" in entidades


def test_a_leitura_das_baixas_por_tratar_e_o_reenvio_pulam_as_linhas_travadas(
    sessao, ligada, politica
):
    chatwoot = ChatwootQueCai()
    chatwoot.fora = True
    deps = _deps(politica, _ctn([_baixa()]), chatwoot=chatwoot)
    pedidos = _pedidos_de_trava(sessao)
    ler_baixas(sessao, deps, AGORA)
    chatwoot.fora = False
    ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    com_pulo = [entidade for entidade, pulando in pedidos if pulando]
    assert com_pulo.count("BaixaCTNRegistro") >= 2  # a leitura das por tratar e o reenvio


# ---- o abate expira o Pix e o link que ficaram com o valor antigo ----


def test_o_abate_expira_o_pix_e_o_link_pendentes_de_valor_antigo(sessao, ligada, politica):
    antigo = _pix_pendente(ligada.cobranca, "tx-antigo", 10020)
    link = _pix_pendente(ligada.cobranca, "lk-antigo", 8000, FormaPagamento.LINK_CTN)
    em_dia = _pix_pendente(ligada.cobranca, "tx-novo", 6680)
    pago = _pix_pendente(ligada.cobranca, "tx-pago", 10020)
    pago.status = StatusPagamento.PAGO
    sessao.commit()
    ctn = _ctn([_baixa()], abertas=_abertas("2026/08", "2026/09"))

    ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert ligada.cobranca.divida_centavos == 6680
    assert antigo.status is StatusPagamento.EXPIRADO
    assert link.status is StatusPagamento.EXPIRADO
    assert em_dia.status is StatusPagamento.PENDENTE  # o valor bate com a dívida nova
    assert pago.status is StatusPagamento.PAGO
    # Nada se apaga: o Pix expirado segue na tabela, com o br_code, como a auditoria.
    assert antigo.br_code


def test_sem_mudanca_nao_expira_o_pix_pendente(sessao, ligada, politica):
    pix = _pix_pendente(ligada.cobranca, "tx", 10020)
    sessao.commit()
    ctn = _ctn([_baixa()], abertas=_abertas("2026/07", "2026/08", "2026/09"))

    ler_baixas(sessao, _deps(politica, ctn), AGORA)

    assert pix.status is StatusPagamento.PENDENTE


# ---- campo acima do tamanho da coluna, e o log sem os parâmetros do SQL ----


def test_baixa_com_campo_acima_da_coluna_e_pulada_sem_travar_as_outras(
    sessao, ligada, politica, caplog
):
    comprida = _baixa("i" * 65, minutos=25)
    ctn = _ctn([_baixa("b-1", minutos=30), comprida, _baixa("b-3", minutos=20)])
    deps = _deps(politica, ctn)

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, deps, AGORA)
        segunda = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert resumo.lidas == 2 and segunda.lidas == 0
    assert {r.id_externo for r in sessao.scalars(select(BaixaCTNRegistro))} == {"b-1", "b-3"}
    assert sessao.get(CursorCTN, CURSOR_BAIXAS).valor == (AGORA - timedelta(minutes=20)).isoformat()
    assert "i" * 65 not in caplog.text


def test_erro_no_tratamento_da_matricula_loga_so_o_tipo_e_os_ids(
    sessao, ligada, politica, caplog, monkeypatch
):
    def quebra(*_args, **_kwargs):
        raise RuntimeError(f"INSERT INTO baixa_ctn [parameters: ('{MATRICULA}', '{NOME}')]")

    monkeypatch.setattr(baixas_modulo, "conferir_cobranca", quebra)

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, _deps(politica, _ctn([_baixa()])), AGORA)

    assert resumo.lidas == 1 and resumo.aplicadas == 0
    assert MATRICULA not in caplog.text and NOME not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    assert "RuntimeError" in caplog.text
    assert _registro(sessao).aplicada_em is None  # fica para o próximo ciclo


def test_a_rotina_nunca_levanta_nem_loga_o_sql_quando_a_gravacao_quebra(
    sessao, ligada, politica, caplog, monkeypatch
):
    def quebra(*_args, **_kwargs):
        raise RuntimeError(f"[parameters: ('{MATRICULA}',)]")

    monkeypatch.setattr(baixas_modulo, "_gravar_baixas", quebra)

    with caplog.at_level("DEBUG"):
        resumo = ler_baixas(sessao, _deps(politica, _ctn([_baixa()])), AGORA)

    assert resumo == ResumoBaixas()
    assert MATRICULA not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


# ---- o cursor só anda depois de gravar tudo ----


def test_se_a_gravacao_da_segunda_baixa_falha_o_cursor_nao_anda_e_a_primeira_nao_duplica(
    sessao, ligada, politica
):
    ctn = _ctn([_baixa("b-1", minutos=30), _baixa("b-2", minutos=20)])
    deps = _deps(politica, ctn)
    falhar = [True]

    @event.listens_for(sessao.get_bind(), "before_cursor_execute")
    def _quebra_na_segunda(_conn, _cursor, statement, parametros, *_resto):
        if (
            falhar[0]
            and statement.lstrip().upper().startswith("INSERT INTO BAIXA_CTN")
            and "b-2" in parametros
        ):
            raise RuntimeError("o disco encheu (simulado)")

    resumo = ler_baixas(sessao, deps, AGORA)

    assert resumo == ResumoBaixas()
    assert sessao.get(CursorCTN, CURSOR_BAIXAS) is None  # o cursor não andou
    # No Postgres o rollback desfaz também a 1ª. O pysqlite confirma o SAVEPOINT mais externo ao
    # soltá-lo, então no SQLite de teste ela pode ter ficado: o que importa é que não se duplique.
    assert len(sessao.scalars(select(BaixaCTNRegistro)).all()) <= 1

    falhar[0] = False
    resumo = ler_baixas(sessao, deps, AGORA + timedelta(minutes=10))

    assert resumo.lidas in (1, 2)
    ids = [r.id_externo for r in sessao.scalars(select(BaixaCTNRegistro))]
    assert sorted(ids) == ["b-1", "b-2"]
    assert sessao.get(CursorCTN, CURSOR_BAIXAS).valor == (AGORA - timedelta(minutes=20)).isoformat()
