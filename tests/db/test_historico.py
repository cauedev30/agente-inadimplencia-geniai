from datetime import UTC, datetime

from sqlalchemy import select

from agente.db.historico import (
    abrir_turno,
    fechar_turno,
    gravar_laudos,
    mensagens_no_teto,
    registrar_mensagem,
    transicionar,
)
from agente.db.modelos import LaudoGravado, Mensagem, Transicao, Turno
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Laudo,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)

MOMENTO = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _registrar(sessao, conversa, origem, chatwoot_id=None, conta_no_teto=False):
    return registrar_mensagem(
        sessao,
        conversa_id=conversa.id,
        origem=origem,
        texto="texto",
        chatwoot_message_id=chatwoot_id,
        conta_no_teto=conta_no_teto,
        momento=MOMENTO,
    )


def test_mensagem_repetida_devolve_none_e_nao_grava(sessao, conversa):
    primeira = _registrar(sessao, conversa, OrigemMensagem.CLIENTE, "cw-1")
    repetida = _registrar(sessao, conversa, OrigemMensagem.CLIENTE, "cw-1")

    assert primeira is not None
    assert repetida is None
    assert len(sessao.scalars(select(Mensagem)).all()) == 1


def test_teto_conta_so_as_mensagens_de_resolucao(sessao, conversa):
    _registrar(sessao, conversa, OrigemMensagem.ABERTURA)
    _registrar(sessao, conversa, OrigemMensagem.CLIENTE, "cw-1")
    _registrar(sessao, conversa, OrigemMensagem.AGENTE, "cw-2", conta_no_teto=True)
    _registrar(sessao, conversa, OrigemMensagem.AGENTE, "cw-3", conta_no_teto=True)
    _registrar(sessao, conversa, OrigemMensagem.FIXA, "cw-4")

    assert mensagens_no_teto(sessao, conversa.id) == 2


def test_turno_grava_laudos_e_resultado(sessao, conversa):
    mensagem = _registrar(sessao, conversa, OrigemMensagem.CLIENTE, "cw-1")
    turno = abrir_turno(sessao, conversa.id, [mensagem.id], Modo.SOMBRA)
    ids = gravar_laudos(
        sessao,
        turno.id,
        BatchJev.JEV1,
        [
            Laudo("quer_retencao", False, 0.12, 0.90),
            Laudo("tom", "neutro", 0.81, 0.75),
            Laudo("intencao_pagar", 0.6, 0.70, 0.65),
        ],
        origem="falso",
    )
    fechar_turno(
        sessao,
        turno.id,
        ResultadoTurno.SOMBRA,
        acao_principal="INFORM_DEBT",
        acoes_anexadas=["GENERATE_PIX"],
        numeros={"divida_centavos": 10020},
        momento=MOMENTO,
    )
    sessao.commit()
    sessao.expire_all()

    salvo = sessao.get(Turno, turno.id)
    assert salvo.mensagens_ids == [mensagem.id]
    assert salvo.resultado is ResultadoTurno.SOMBRA
    assert salvo.acoes_anexadas == ["GENERATE_PIX"]
    assert salvo.numeros == {"divida_centavos": 10020}
    assert salvo.fechado_em == MOMENTO
    assert len(ids) == 3
    valores = {laudo.pergunta: laudo.valor for laudo in sessao.scalars(select(LaudoGravado))}
    assert valores == {"quer_retencao": False, "tom": "neutro", "intencao_pagar": 0.6}
    # A origem separa os laudos da falsa dos do Jev real na calibração.
    assert {laudo.origem for laudo in sessao.scalars(select(LaudoGravado))} == {"falso"}


def test_transicao_grava_o_laudo_que_a_causou(sessao, conversa):
    turno = abrir_turno(sessao, conversa.id, [], Modo.ATIVO)
    [laudo_id] = gravar_laudos(
        sessao, turno.id, BatchJev.JEV1, [Laudo("risco_juridico", True, 0.91, 0.80)], "falso"
    )

    transicionar(
        sessao,
        conversa,
        EstadoConversa.ATENDIMENTO_HUMANO,
        motivo=MotivoHumano.JURIDICO,
        laudo_id=laudo_id,
        momento=MOMENTO,
    )
    sessao.commit()

    assert conversa.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert conversa.motivo_humano is MotivoHumano.JURIDICO
    transicao = sessao.scalars(select(Transicao)).one()
    assert transicao.de is EstadoConversa.INICIADO
    assert transicao.para is EstadoConversa.ATENDIMENTO_HUMANO
    assert transicao.laudo_id == laudo_id


def test_transicao_sem_humano_limpa_o_motivo(sessao, conversa):
    transicionar(sessao, conversa, EstadoConversa.ATENDIMENTO_HUMANO, MotivoHumano.TOM, None)
    transicionar(sessao, conversa, EstadoConversa.EM_NEGOCIACAO, None, None)

    assert conversa.motivo_humano is None
    assert len(sessao.scalars(select(Transicao)).all()) == 2
