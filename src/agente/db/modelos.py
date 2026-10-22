"""Tabelas do agente. Tudo o que entra e sai de uma conversa fica gravado (fase 0)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, TypeDecorator, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from agente.politica import Regua
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    FormaPagamento,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
    StatusPagamento,
)


class MomentoUTC(TypeDecorator):
    """Grava em UTC e devolve com fuso. Recusa horário sem fuso."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, valor: datetime | None, dialect) -> datetime | None:
        if valor is None:
            return None
        if valor.tzinfo is None:
            raise ValueError("horário sem fuso: todo horário é gravado com fuso")
        return valor.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, valor: datetime | None, dialect) -> datetime | None:
        return None if valor is None else valor.replace(tzinfo=UTC)


def _enum(tipo) -> Enum:
    return Enum(tipo, native_enum=False, length=32, validate_strings=True)


def _agora() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: MomentoUTC, dict[str, Any]: JSON, list[Any]: JSON}


class Cliente(Base):
    __tablename__ = "cliente"

    id: Mapped[int] = mapped_column(primary_key=True)
    matricula_ctn: Mapped[str] = mapped_column(String(64), unique=True)
    nome: Mapped[str] = mapped_column(String(200))
    telefone: Mapped[str] = mapped_column(String(20), index=True)
    criado_em: Mapped[datetime] = mapped_column(default=_agora)

    cobrancas: Mapped[list["Cobranca"]] = relationship(back_populates="cliente")


class Cobranca(Base):
    __tablename__ = "cobranca"
    __table_args__ = (
        # Só uma cobrança ativa por cliente.
        Index(
            "uq_cobranca_ativa_por_cliente",
            "cliente_id",
            unique=True,
            sqlite_where=text("status = 'ATIVA'"),
            postgresql_where=text("status = 'ATIVA'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("cliente.id"))
    regua: Mapped[Regua] = mapped_column(_enum(Regua))
    mensalidades_abertas: Mapped[int]
    divida_centavos: Mapped[int]
    propenso_cartao: Mapped[bool]
    campanha: Mapped[str] = mapped_column(String(100))
    status: Mapped[StatusCobranca] = mapped_column(
        _enum(StatusCobranca), default=StatusCobranca.ATIVA
    )
    criada_em: Mapped[datetime] = mapped_column(default=_agora)

    cliente: Mapped[Cliente] = relationship(back_populates="cobrancas")
    conversas: Mapped[list["Conversa"]] = relationship(back_populates="cobranca")
    pagamentos: Mapped[list["Pagamento"]] = relationship(back_populates="cobranca")


class Conversa(Base):
    __tablename__ = "conversa"

    id: Mapped[int] = mapped_column(primary_key=True)
    cobranca_id: Mapped[int] = mapped_column(ForeignKey("cobranca.id"))
    chatwoot_conversa_id: Mapped[int | None] = mapped_column(unique=True)
    estado: Mapped[EstadoConversa] = mapped_column(
        _enum(EstadoConversa), default=EstadoConversa.INICIADO
    )
    motivo_humano: Mapped[MotivoHumano | None] = mapped_column(_enum(MotivoHumano))
    human_owned: Mapped[bool] = mapped_column(default=False)
    degrau_atual: Mapped[int] = mapped_column(default=0)
    objecoes_sem_desconto: Mapped[int] = mapped_column(default=0)
    forma_enviada: Mapped[FormaPagamento | None] = mapped_column(_enum(FormaPagamento))
    turno_em: Mapped[datetime | None] = mapped_column(index=True)
    criada_em: Mapped[datetime] = mapped_column(default=_agora)

    cobranca: Mapped[Cobranca] = relationship(back_populates="conversas")
    mensagens: Mapped[list["Mensagem"]] = relationship(back_populates="conversa")
    turnos: Mapped[list["Turno"]] = relationship(back_populates="conversa")


class Mensagem(Base):
    __tablename__ = "mensagem"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversa_id: Mapped[int] = mapped_column(ForeignKey("conversa.id"), index=True)
    origem: Mapped[OrigemMensagem] = mapped_column(_enum(OrigemMensagem))
    texto: Mapped[str] = mapped_column(Text)
    chatwoot_message_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    conta_no_teto: Mapped[bool]
    momento: Mapped[datetime]

    conversa: Mapped[Conversa] = relationship(back_populates="mensagens")


class Turno(Base):
    __tablename__ = "turno"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversa_id: Mapped[int] = mapped_column(ForeignKey("conversa.id"), index=True)
    modo: Mapped[Modo] = mapped_column(_enum(Modo))
    mensagens_ids: Mapped[list[Any]] = mapped_column(default=list)
    acao_principal: Mapped[str | None] = mapped_column(String(32))
    acoes_anexadas: Mapped[list[Any]] = mapped_column(default=list)
    numeros: Mapped[dict[str, Any]] = mapped_column(default=dict)
    texto_enviado: Mapped[str | None] = mapped_column(Text)
    resultado: Mapped[ResultadoTurno | None] = mapped_column(_enum(ResultadoTurno))
    aberto_em: Mapped[datetime] = mapped_column(default=_agora)
    fechado_em: Mapped[datetime | None]

    conversa: Mapped[Conversa] = relationship(back_populates="turnos")
    laudos: Mapped[list["LaudoGravado"]] = relationship(back_populates="turno")


class LaudoGravado(Base):
    """Um laudo do Jev gravado: a pergunta, a resposta, a probabilidade e a confiança."""

    __tablename__ = "laudo"

    id: Mapped[int] = mapped_column(primary_key=True)
    turno_id: Mapped[int] = mapped_column(ForeignKey("turno.id"), index=True)
    batch: Mapped[BatchJev] = mapped_column(_enum(BatchJev))
    pergunta: Mapped[str] = mapped_column(String(64))
    # Em JSON porque a resposta pode ser sim/não, nota ou escolha.
    valor: Mapped[Any] = mapped_column(JSON)
    probabilidade: Mapped[float]
    confianca: Mapped[float]

    turno: Mapped[Turno] = relationship(back_populates="laudos")


class Transicao(Base):
    """Toda mudança de estado, com o laudo que a causou (auditoria)."""

    __tablename__ = "transicao"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversa_id: Mapped[int] = mapped_column(ForeignKey("conversa.id"), index=True)
    de: Mapped[EstadoConversa] = mapped_column(_enum(EstadoConversa))
    para: Mapped[EstadoConversa] = mapped_column(_enum(EstadoConversa))
    motivo: Mapped[MotivoHumano | None] = mapped_column(_enum(MotivoHumano))
    laudo_id: Mapped[int | None] = mapped_column(ForeignKey("laudo.id"))
    momento: Mapped[datetime]


class Pagamento(Base):
    __tablename__ = "pagamento"

    id: Mapped[int] = mapped_column(primary_key=True)
    cobranca_id: Mapped[int] = mapped_column(ForeignKey("cobranca.id"), index=True)
    forma: Mapped[FormaPagamento] = mapped_column(_enum(FormaPagamento))
    valor_centavos: Mapped[int]
    referencia: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[StatusPagamento] = mapped_column(
        _enum(StatusPagamento), default=StatusPagamento.PENDENTE
    )
    criado_em: Mapped[datetime] = mapped_column(default=_agora)
    expira_em: Mapped[datetime | None]
    pago_em: Mapped[datetime | None]
    baixa_ctn_em: Mapped[datetime | None]

    cobranca: Mapped[Cobranca] = relationship(back_populates="pagamentos")
