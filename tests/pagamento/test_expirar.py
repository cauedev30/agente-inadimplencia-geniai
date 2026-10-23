"""O Pix vencido (tarefa 6.6): vira expirado, sem mensagem e sem novo contato (G2.4)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from agente.db.modelos import Base, Cliente, Cobranca, Mensagem, Pagamento
from agente.db.sessao import criar_sessao
from agente.pagamento.expirar import expirar_pix
from agente.politica import Regua
from agente.tipos import FormaPagamento, StatusPagamento
from agente.turno.fila import Trabalhador

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
