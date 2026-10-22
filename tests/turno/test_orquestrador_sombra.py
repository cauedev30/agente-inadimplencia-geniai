"""O turno com o Jev 1, o Jev 2 e as regras de ação (tarefa 4.5). Em sombra, nada sai."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Base, Cliente, Cobranca, Conversa, LaudoGravado, Transicao, Turno
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevComPrazo, JevFalso, responder
from agente.politica import Regua
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.orquestrador import Dependencias, criar_processador, processar_turno

from .test_avaliacao import SoJev1
from .test_orquestrador_gates import SEGUNDA_10H

OBJECAO = [responder("objecao_valor", True), responder("acao", "OFFER_DISCOUNT")]


def _deps(politica, modo=Modo.SOMBRA, regras=(), jev=None):
    return Dependencias(
        politica=politica,
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=jev or JevFalso(list(regras), registrar=True),
        ctn=CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)]),
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _cliente(sessao, conversa, texto="quanto eu devo?", mid="cw-1"):
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.CLIENTE, texto, mid, False, SEGUNDA_10H)


def _turno(sessao):
    return sessao.scalars(select(Turno)).one()


def _laudos(sessao, batch):
    return sessao.scalars(select(LaudoGravado).where(LaudoGravado.batch == batch)).all()


def test_em_sombra_grava_os_laudos_e_o_que_teria_feito(sessao, ligada, politica):
    deps = _deps(politica)
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    turno = _turno(sessao)
    assert turno.modo is Modo.SOMBRA
    assert len(_laudos(sessao, BatchJev.JEV1)) == 13
    [jev2] = _laudos(sessao, BatchJev.JEV2)
    assert (jev2.pergunta, jev2.valor, jev2.turno_id) == ("acao", "INFORM_DEBT", turno.id)
    assert {laudo.origem for laudo in turno.laudos} == {"falso"}
    assert turno.humano_na_conversa is False
    # A cobrança fictícia não é propensa: o débito vai com o Pix integral (D34, D37).
    assert turno.acao_principal == "INFORM_DEBT"
    assert turno.acoes_anexadas == ["GENERATE_PIX"]
    assert turno.numeros == {"divida_centavos": 10020, "valor_pix_centavos": 10020}


def test_em_sombra_a_objecao_na_r3_teria_liberado_o_degrau_sem_mudar_o_estado(
    sessao, ligada, politica
):
    deps = _deps(politica, regras=OBJECAO)
    _cliente(sessao, ligada, "tá caro, tem desconto?")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    turno = _turno(sessao)
    assert turno.acao_principal == "OFFER_DISCOUNT"
    assert turno.acoes_anexadas == ["SEND_CTN_LINK"]
    assert turno.numeros == {
        "divida_centavos": 10020,
        "degrau": 1,
        "desconto_centavos": 3000,
        "valor_final_centavos": 7020,
        "valor_link_centavos": 7020,
    }
    assert (ligada.degrau_atual, ligada.estado) == (0, EstadoConversa.INICIADO)
    assert deps.chatwoot.chamadas == []


def test_em_sombra_o_link_do_propenso_leva_o_degrau_atual(sessao, ligada, politica):
    # O desconto já liberado continua no link (D34); o Pix seria integral.
    ligada.cobranca.propenso_cartao = True
    ligada.degrau_atual = 2
    deps = _deps(politica)
    _cliente(sessao, ligada)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("INFORM_DEBT", ["SEND_CTN_LINK"])
    assert turno.numeros == {"divida_centavos": 10020, "valor_link_centavos": 4020}


def test_em_sombra_o_gatilho_so_fica_registrado(sessao, ligada, politica):
    deps = _deps(politica, regras=[responder("quer_retencao", True, 0.92)])
    _cliente(sessao, ligada, "quero cancelar o cartão")

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    assert ligada.estado is EstadoConversa.INICIADO
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("HUMAN:RETENCAO", [])
    # Na retenção, o robô se cala no mesmo turno: o Jev 2 nem roda.
    assert len(deps.jev.chamadas) == 1
    assert _laudos(sessao, BatchJev.JEV2) == []


def test_em_sombra_o_jev_indisponivel_fica_registrado_no_turno(sessao, ligada, politica):
    jev = JevComPrazo(JevFalso(falha=ConnectionError("gateway fora")), politica.jev_timeout_ms)
    deps = _deps(politica, jev=jev)
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    assert ligada.estado is EstadoConversa.INICIADO
    turno = _turno(sessao)
    assert turno.acao_principal == "HUMAN:JEV_INDISPONIVEL"
    assert "gateway fora" in turno.numeros["erro"]


def test_jev2_indisponivel_guarda_a_leitura_do_jev1(sessao, ligada, politica):
    deps = _deps(politica, jev=SoJev1())
    _cliente(sessao, ligada)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert len(_laudos(sessao, BatchJev.JEV1)) == 13
    assert _turno(sessao).acao_principal == "HUMAN:JEV_INDISPONIVEL"


def test_o_jev_le_o_historico_e_o_estado_da_conversa(sessao, ligada, politica):
    deps = _deps(politica)
    registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.ABERTURA, "[abertura]", None, False, SEGUNDA_10H
    )
    _cliente(sessao, ligada, "quanto eu devo?")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    _, estado = deps.jev.chamadas[0]
    assert estado.historico == (("ABERTURA", "[abertura]"), ("CLIENTE", "quanto eu devo?"))
    assert (estado.regua, estado.divida_centavos) == (ligada.cobranca.regua, 10020)
    assert estado.estado_conversa is EstadoConversa.INICIADO
    assert estado.oferta_atual is None


def test_o_jev_ve_a_oferta_do_degrau_atual(sessao, ligada, politica):
    ligada.degrau_atual = 2
    deps = _deps(politica)
    _cliente(sessao, ligada)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    oferta = deps.jev.chamadas[0][1].oferta_atual
    assert (oferta.degrau, oferta.desconto_centavos, oferta.valor_final_centavos) == (
        2,
        6000,
        4020,
    )


def test_gate_que_nao_segue_nao_chama_o_jev(sessao, ligada, politica):
    deps = _deps(politica)
    ligada.cobranca.status = StatusCobranca.PAGA
    _cliente(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
    assert deps.jev.chamadas == []


def test_em_sombra_o_humano_na_conversa_nao_barra_a_avaliacao(sessao, ligada, politica):
    # Padrão da T7: a sombra lê as conversas reais enquanto os humanos atendem.
    deps = _deps(politica)
    ligada.human_owned = True
    registrar_mensagem(
        sessao,
        ligada.id,
        OrigemMensagem.HUMANO,
        "Oi, aqui é a atendente",
        "cw-0",
        False,
        SEGUNDA_10H,
    )
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    turno = _turno(sessao)
    assert turno.humano_na_conversa is True
    assert turno.acao_principal == "INFORM_DEBT"
    assert len(turno.laudos) == 14
    assert ("HUMANO", "Oi, aqui é a atendente") in deps.jev.chamadas[0][1].historico
    assert ligada.human_owned is True


def test_ativo_o_humano_na_conversa_barra_o_jev(sessao, ligada, politica):
    deps = _deps(politica, Modo.ATIVO)
    ligada.human_owned = True
    _cliente(sessao, ligada)

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
    assert deps.jev.chamadas == []
    assert _turno(sessao).humano_na_conversa is True


# Modo ativo: a saída para o humano acontece, e o backend executa a ação; o envio é da 5.3.


def test_ativo_o_gatilho_faz_o_handoff_com_o_laudo_da_causa(sessao, ligada, politica):
    deps = _deps(politica, Modo.ATIVO, [responder("risco_juridico", True, 0.91, 0.80)])
    _cliente(sessao, ligada, "vou no Procon")

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert [c[0] for c in deps.chatwoot.chamadas][:4] == [
        "nota_privada",
        "etiquetar",
        "atribuir",
        "mudar_status",
    ]
    assert "risco_juridico" in deps.chatwoot.chamadas[0][2]
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.JURIDICO,
    )
    transicao = sessao.scalars(select(Transicao)).one()
    causa = sessao.get(LaudoGravado, transicao.laudo_id)
    assert (causa.pergunta, causa.probabilidade) == ("risco_juridico", 0.91)
    assert _turno(sessao).acao_principal == "HUMAN:JURIDICO"


def test_ativo_o_jev_indisponivel_vai_para_o_humano(sessao, ligada, politica):
    jev = JevComPrazo(JevFalso(demora_s=0.5), timeout_ms=20)
    deps = _deps(politica, Modo.ATIVO, jev=jev)
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.JEV_INDISPONIVEL
    assert sessao.scalars(select(Transicao)).one().laudo_id is None


def test_ativo_jev2_indisponivel_vai_para_o_humano(sessao, ligada, politica):
    deps = _deps(politica, Modo.ATIVO, jev=SoJev1())
    _cliente(sessao, ligada)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.JEV_INDISPONIVEL
    assert sessao.scalars(select(Transicao)).one().laudo_id is None
    assert len(_laudos(sessao, BatchJev.JEV1)) == 13


def test_ativo_objecao_sem_desconto_aponta_o_laudo_da_objecao(sessao, ligada, politica):
    ligada.cobranca.regua = Regua.NR
    ligada.objecoes_sem_desconto = politica.objecoes_sem_desconto_ate_humano - 1
    deps = _deps(politica, Modo.ATIVO, OBJECAO)
    _cliente(sessao, ligada, "sem desconto eu não pago")

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.OBJECAO_SEM_DESCONTO
    causa = sessao.get(LaudoGravado, sessao.scalars(select(Transicao)).one().laudo_id)
    assert (causa.batch, causa.pergunta) == (BatchJev.JEV1, "objecao_valor")


def test_ativo_a_baixa_confianca_do_jev2_vai_para_o_humano(sessao, ligada, politica):
    deps = _deps(politica, Modo.ATIVO, [responder("acao", "GENERATE_PIX", 0.80)])
    _cliente(sessao, ligada, "manda o pix")

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.BAIXA_CONFIANCA
    transicao = sessao.scalars(select(Transicao)).one()
    assert sessao.get(LaudoGravado, transicao.laudo_id).batch is BatchJev.JEV2


def test_ativo_a_acao_e_executada_sem_envio(sessao, ligada, politica):
    deps = _deps(politica, Modo.ATIVO, OBJECAO)
    _cliente(sessao, ligada, "tá caro")

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
    assert _turno(sessao).acao_principal == "OFFER_DISCOUNT"
    assert [valor for _, valor, _ in deps.ctn.links.values()] == [7020]


# Pronto quando: o serviço da tarefa 3.7 grava o laudo de toda rajada real.


def test_o_servico_grava_o_laudo_de_toda_rajada(politica):
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="F-9", nome="Cliente Fictício", telefone="+5583999990009")
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="outubro",
        )
        sessao.add(Conversa(cobranca=cobranca))
        sessao.commit()
    deps = _deps(politica)
    relogio = {"agora": SEGUNDA_10H}
    app = criar_app(
        fabrica,
        politica,
        Config(database_url="sqlite://", webhook_segredo="s3gredo"),
        processar=criar_processador(deps),
        relogio=lambda: relogio["agora"],
        iniciar_trabalhador=False,
    )
    evento = {
        "event": "message_created",
        "content": "oi, recebi a mensagem",
        "message_type": "incoming",
        "conversation": {"id": 777},
        "sender": {"type": "contact", "phone_number": "+5583999990009"},
    }
    with TestClient(app) as cliente_http:
        for mensagem_id in (1, 2):
            resposta = cliente_http.post(
                "/webhooks/chatwoot/s3gredo", json={**evento, "id": mensagem_id}
            )
            assert resposta.json() == {"resultado": "agendado"}
        relogio["agora"] = SEGUNDA_10H + timedelta(seconds=politica.rajada_segundos + 1)
        assert app.state.trabalhador.rodar_uma_vez() == 1

    with fabrica() as sessao:
        turno = sessao.scalars(select(Turno)).one()
        assert (turno.modo, turno.resultado, len(turno.mensagens_ids)) == (
            Modo.SOMBRA,
            ResultadoTurno.SOMBRA,
            2,
        )
        assert len(turno.laudos) == 14
    assert deps.chatwoot.chamadas == []
