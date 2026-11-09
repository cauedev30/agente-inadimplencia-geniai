from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from agente.db.modelos import Base, Cliente, Cobranca, CursorCTN, Mensagem
from agente.politica import Regua
from agente.tipos import EstadoConversa, OrigemMensagem, StatusCobranca

from ..conftest import alembic_config


def _mensagem(conversa, chatwoot_id):
    return Mensagem(
        conversa=conversa,
        origem=OrigemMensagem.CLIENTE,
        texto="oi",
        chatwoot_message_id=chatwoot_id,
        conta_no_teto=False,
        momento=datetime(2026, 10, 22, 12, tzinfo=UTC),
    )


def test_chatwoot_message_id_repetido_e_recusado(sessao, conversa):
    sessao.add(_mensagem(conversa, "cw-1"))
    sessao.commit()
    sessao.add(_mensagem(conversa, "cw-1"))
    with pytest.raises(IntegrityError):
        sessao.commit()


def test_mensagem_sem_chatwoot_id_pode_repetir(sessao, conversa):
    sessao.add_all([_mensagem(conversa, None), _mensagem(conversa, None)])
    sessao.commit()


def _cobranca(cliente, status=StatusCobranca.ATIVA):
    return Cobranca(
        cliente=cliente,
        regua=Regua.R1,
        mensalidades_abertas=1,
        divida_centavos=3340,
        propenso_cartao=False,
        campanha="teste",
        status=status,
    )


def test_so_uma_cobranca_ativa_por_cliente(sessao):
    cliente = Cliente(matricula_ctn="M-0002", nome="Outro Fictício", telefone="+5583900000002")
    sessao.add_all([_cobranca(cliente, StatusCobranca.ENCERRADA), _cobranca(cliente)])
    sessao.commit()
    sessao.add(_cobranca(cliente))
    with pytest.raises(IntegrityError):
        sessao.commit()


def test_momento_volta_com_fuso_em_utc(sessao, conversa):
    recife = timezone(timedelta(hours=-3))
    sessao.add(
        Mensagem(
            conversa=conversa,
            origem=OrigemMensagem.CLIENTE,
            texto="oi",
            conta_no_teto=False,
            momento=datetime(2026, 10, 22, 9, tzinfo=recife),
        )
    )
    sessao.commit()
    sessao.expire_all()
    salva = sessao.query(Mensagem).one()
    assert salva.momento == datetime(2026, 10, 22, 12, tzinfo=UTC)
    assert salva.momento.tzinfo is not None


def test_momento_sem_fuso_e_recusado(sessao, conversa):
    sessao.add(
        Mensagem(
            conversa=conversa,
            origem=OrigemMensagem.CLIENTE,
            texto="oi",
            conta_no_teto=False,
            momento=datetime(2026, 10, 22, 9),
        )
    )
    with pytest.raises(Exception, match="fuso"):
        sessao.commit()


def test_conversa_comeca_iniciada(conversa):
    assert conversa.estado is EstadoConversa.INICIADO
    assert conversa.human_owned is False
    assert conversa.degrau_atual == 0


def test_migracao_sobe_desce_e_bate_com_os_modelos(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    config = alembic_config(url)

    command.upgrade(config, "head")
    motor = create_engine(url)
    with motor.connect() as conexao:
        diferencas = compare_metadata(MigrationContext.configure(conexao), Base.metadata)
    assert diferencas == []

    command.downgrade(config, "base")
    assert inspect(motor).get_table_names() == ["alembic_version"]
    motor.dispose()


def test_migracao_0012_cria_as_colunas_da_conferencia_e_o_cursor(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm12.db').as_posix()}"
    config = alembic_config(url)

    command.upgrade(config, "0011")
    motor = create_engine(url)
    assert "conferida_em" not in {c["name"] for c in inspect(motor).get_columns("cobranca")}
    assert "cursor_ctn" not in inspect(motor).get_table_names()

    command.upgrade(config, "0012")
    colunas = {c["name"]: c for c in inspect(motor).get_columns("cobranca")}
    for nome in ("conferida_em", "meses_em_aberto", "encerrada_motivo"):
        assert colunas[nome]["nullable"] is True
    assert [c["name"] for c in inspect(motor).get_columns("cursor_ctn")] == ["nome", "valor"]
    assert inspect(motor).get_pk_constraint("cursor_ctn")["constrained_columns"] == ["nome"]

    command.downgrade(config, "0011")
    assert "conferida_em" not in {c["name"] for c in inspect(motor).get_columns("cobranca")}
    assert "cursor_ctn" not in inspect(motor).get_table_names()
    motor.dispose()


def test_cobranca_guarda_a_conferencia_e_o_cursor_grava_o_valor(sessao, conversa):
    cobranca = conversa.cobranca
    cobranca.conferida_em = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    cobranca.meses_em_aberto = ["2026/09", "2026/10"]
    cobranca.encerrada_motivo = "saiu_do_retrato"
    sessao.add(CursorCTN(nome="baixas", valor="2026-11-09T12:00:00+00:00"))
    sessao.commit()
    sessao.expire_all()

    assert cobranca.meses_em_aberto == ["2026/09", "2026/10"]
    assert cobranca.conferida_em == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert sessao.get(CursorCTN, "baixas").valor == "2026-11-09T12:00:00+00:00"


def test_cobranca_nova_nasce_sem_conferencia(sessao, conversa):
    cobranca = conversa.cobranca
    assert cobranca.conferida_em is None
    assert cobranca.meses_em_aberto is None
    assert cobranca.encerrada_motivo is None
