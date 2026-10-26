"""O Pix vencido (tarefa 6.6): vira expirado, sem mensagem e sem novo contato (G2.4)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from agente.db.modelos import Base, Cliente, Cobranca, Mensagem, Pagamento
from agente.db.sessao import criar_sessao
from agente.pagamento.expirar import expirar_pendentes, expirar_pix
from agente.politica import Regua
from agente.tipos import FormaPagamento, StatusPagamento
from agente.turno.fila import Trabalhador, a_cada

AGORA = datetime(2026, 10, 26, 13, tzinfo=UTC)


def _pix(cobranca, horas_atras, *, status=StatusPagamento.PENDENTE, referencia="tx", forma=None):
    criado = AGORA - timedelta(hours=horas_atras)
    pagamento = Pagamento(
        forma=forma or FormaPagamento.PIX,
        valor_centavos=cobranca.divida_centavos,
        referencia=referencia,
        status=status,
        criado_em=criado,
        expira_em=criado + timedelta(hours=24),
    )
    cobranca.pagamentos.append(pagamento)
    return pagamento


def test_o_pix_de_25h_atras_expira_e_o_de_23h_nao(sessao, conversa):
    velho = _pix(conversa.cobranca, 25, referencia="velho")
    novo = _pix(conversa.cobranca, 23, referencia="novo")
    sessao.commit()

    assert expirar_pix(sessao, AGORA) == 1

    assert velho.status is StatusPagamento.EXPIRADO
    assert novo.status is StatusPagamento.PENDENTE


def test_nenhuma_mensagem_e_enviada(sessao, conversa):
    _pix(conversa.cobranca, 25)
    sessao.commit()

    expirar_pix(sessao, AGORA)

    assert sessao.scalars(select(Mensagem)).all() == []
    assert conversa.turno_em is None


def test_o_pago_e_o_que_ja_expirou_ficam_como_estao(sessao, conversa):
    pago = _pix(conversa.cobranca, 30, status=StatusPagamento.PAGO, referencia="a")
    expirado = _pix(conversa.cobranca, 30, status=StatusPagamento.EXPIRADO, referencia="b")
    sessao.commit()

    assert expirar_pix(sessao, AGORA) == 0
    assert (pago.status, expirado.status) == (StatusPagamento.PAGO, StatusPagamento.EXPIRADO)


def test_o_link_do_ctn_nao_e_um_pix(sessao, conversa):
    link = _pix(conversa.cobranca, 30, forma=FormaPagamento.LINK_CTN)
    sessao.commit()

    assert expirar_pix(sessao, AGORA) == 0
    assert link.status is StatusPagamento.PENDENTE


def test_o_laco_do_trabalhador_roda_expirar_pix():
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cobranca = Cobranca(
            cliente=Cliente(matricula_ctn="M-9", nome="Fulano Fictício", telefone="+5583900000009"),
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="teste",
        )
        sessao.add(cobranca)
        _pix(cobranca, 25)
        sessao.commit()

    trabalhador = Trabalhador(fabrica, lambda *_: None, lambda: AGORA, rotinas=[expirar_pix])
    trabalhador.rodar_uma_vez()

    with fabrica() as sessao:
        assert sessao.scalars(select(Pagamento)).one().status is StatusPagamento.EXPIRADO


def test_o_pago_que_chega_entre_a_leitura_e_a_gravacao_nao_vira_expirado(sessao, conversa):
    pix = _pix(conversa.cobranca, 25)
    sessao.commit()
    motor = sessao.get_bind()
    chegou = []

    def webhook_grava_pago(conexao, cursor, statement, parametros, contexto, executemany):
        # O webhook commita o PAGO um instante antes do UPDATE da rotina chegar ao banco.
        if statement.startswith("UPDATE pagamento") and not chegou:
            chegou.append(True)
            conexao.exec_driver_sql("UPDATE pagamento SET status = 'PAGO'")

    event.listen(motor, "before_cursor_execute", webhook_grava_pago)
    try:
        expirar_pix(sessao, AGORA)
    finally:
        event.remove(motor, "before_cursor_execute", webhook_grava_pago)

    assert chegou
    sessao.refresh(pix)
    assert pix.status is StatusPagamento.PAGO


def test_o_handoff_que_expira_os_pendentes_tambem_nao_sobrescreve_o_pago(sessao, conversa):
    pago = _pix(conversa.cobranca, 1, status=StatusPagamento.PAGO, referencia="a")
    pendente = _pix(conversa.cobranca, 1, referencia="b")
    link = _pix(conversa.cobranca, 1, forma=FormaPagamento.LINK_CTN, referencia="c")
    sessao.commit()

    assert expirar_pendentes(sessao, conversa.cobranca) == 1

    assert (pago.status, pendente.status, link.status) == (
        StatusPagamento.PAGO,
        StatusPagamento.EXPIRADO,
        StatusPagamento.PENDENTE,
    )


def test_os_pix_da_mesma_cobranca_sao_travados_em_ordem_de_id_antes_do_update(sessao, conversa):
    # Dois UPDATEs de várias linhas (a varredura e o handoff) não podem se cruzar em ciclo.
    _pix(conversa.cobranca, 25, referencia="a")
    _pix(conversa.cobranca, 26, referencia="b")
    sessao.commit()
    comandos = []

    def capturar(estado):
        comandos.append(estado.statement.compile(dialect=postgresql.dialect()).string)

    event.listen(sessao, "do_orm_execute", capturar)

    assert expirar_pix(sessao, AGORA) == 2
    assert expirar_pendentes(sessao, conversa.cobranca) == 0

    travas = [c for c in comandos if "FOR NO KEY UPDATE" in c]
    assert len(travas) == 2 and all("ORDER BY pagamento.id" in c for c in travas)
    assert comandos[0] in travas and comandos[1].startswith("UPDATE pagamento")


def test_a_rotina_espacada_roda_uma_vez_por_intervalo(sessao):
    chamadas = []
    rotina = a_cada(timedelta(minutes=1), lambda s, agora: chamadas.append(agora))

    for segundos in (0, 1, 30, 59, 60, 61, 125):
        rotina(sessao, AGORA + timedelta(seconds=segundos))

    assert chamadas == [AGORA, AGORA + timedelta(seconds=60), AGORA + timedelta(seconds=125)]
