"""Tabelas do agente. Tudo o que entra e sai de uma conversa fica gravado (fase 0)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, TypeDecorator, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from agente.politica import Regua
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    EstadoEnvio,
    FormaHistorico,
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
    # Os meses em atraso que o retrato da importação trouxe (a importação diária o atualiza). Só a
    # importação mexe nele: a conferência reescreve `mensalidades_abertas`, e a rotina das baixas
    # precisa do retrato para só quitar quem pagou tudo o que ele dizia. Nulo = anterior à coluna.
    mensalidades_retrato: Mapped[int | None]
    status: Mapped[StatusCobranca] = mapped_column(
        _enum(StatusCobranca), default=StatusCobranca.ATIVA
    )
    criada_em: Mapped[datetime] = mapped_column(default=_agora)
    # Os erros no CPF da página do webchat; ao chegar no limite do politica.toml, o link bloqueia.
    webchat_tentativas_cpf: Mapped[int] = mapped_column(default=0)
    # Até quando o link está bloqueado por erros no CPF; passado o prazo, as tentativas zeram.
    webchat_bloqueado_ate: Mapped[datetime | None]
    # Quando o cliente acertou o CPF na página; a amarração só aceita a verificada há pouco.
    webchat_verificado_em: Mapped[datetime | None]
    # A conversa do webchat já amarrada a esta cobrança (a amarração repetida não duplica a nota).
    webchat_conversa_id: Mapped[int | None]
    # Quando o CTN foi consultado para este cliente antes do 1º envio do dia (a conferência);
    # nulo = nunca conferida, e os valores são os do retrato diário.
    conferida_em: Mapped[datetime | None]
    # Os meses em aberto que a conferência leu, como `AAAA/MM`; nulo até conferir.
    meses_em_aberto: Mapped[list[Any] | None] = mapped_column(JSON(none_as_null=True))
    # Por que a cobrança deixou de ser ATIVA sem pagamento nosso (ex.: "saiu_do_retrato").
    encerrada_motivo: Mapped[str | None] = mapped_column(String(40))
    # Quando a importação a encerrou; a que voltou ao retrato em poucos dias é reativada.
    encerrada_em: Mapped[datetime | None]

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
    # Os turnos seguidos que falharam por uma integração; o turno que fecha zera (tarefa 5.3).
    falhas_turno: Mapped[int] = mapped_column(default=0)
    # Os turnos seguidos que falharam por erro de código; zera quando um turno termina (ou desiste).
    erros_turno: Mapped[int] = mapped_column(default=0)
    forma_enviada: Mapped[FormaPagamento | None] = mapped_column(_enum(FormaPagamento))
    # Quando o próximo turno roda: a rajada empurra para frente a cada mensagem (tarefa 3.4).
    turno_em: Mapped[datetime | None] = mapped_column(index=True)
    # A última mensagem do cliente que um turno já consumiu.
    ultima_tomada_id: Mapped[int | None]
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
    # A mensagem do agente é gravada PENDENTE antes de ir ao Chatwoot e vira ENVIADA quando sai.
    envio: Mapped[EstadoEnvio] = mapped_column(_enum(EstadoEnvio), default=EstadoEnvio.ENVIADA)
    # Os anexos como vieram do Chatwoot: [{"tipo": "audio"}, ...]. Os gates conferem o tipo.
    anexos: Mapped[list[Any]] = mapped_column(default=list)

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
    # As tentativas do LLM, o que o Jev 3 reprovou e o que saiu (tarefa 5.3).
    redacao: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resultado: Mapped[ResultadoTurno | None] = mapped_column(_enum(ResultadoTurno))
    # Um humano já atendia: em sombra, o turno roda assim mesmo e fica marcado (T7).
    humano_na_conversa: Mapped[bool] = mapped_column(default=False)
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
    # "falso" ou o identificador do Jev real: a calibração usa só os laudos do real.
    origem: Mapped[str] = mapped_column(String(64))
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
    # O copia e cola do Pix, para o 2º toque no botão reaproveitar o que ainda vale.
    br_code: Mapped[str | None] = mapped_column(Text)
    baixa_ctn_em: Mapped[datetime | None]
    # O que a confirmação deixou para depois do commit (baixa, mensagem): a rotina do Trabalhador
    # refaz até acabar, com espera crescente entre as tentativas (pagamento/concluir.py).
    pendencias: Mapped[list[Any]] = mapped_column(default=list)
    tentativas_conclusao: Mapped[int] = mapped_column(default=0)
    proxima_tentativa_em: Mapped[datetime | None] = mapped_column(index=True)
    # Quando as tentativas da conclusão se esgotaram e o atendente foi chamado: o /saude conta os
    # pagamentos com isto e ainda pendentes.
    atencao_em: Mapped[datetime | None]
    # O valor que o banco ou o CTN avisou quando não bateu com o do pagamento: o pagamento fica
    # pendente, vai para o humano e o Pix não é reaproveitado.
    divergente_centavos: Mapped[int | None]

    cobranca: Mapped[Cobranca] = relationship(back_populates="pagamentos")


class AmarracaoWebchat(Base):
    """Uma amarração do webchat que o Chatwoot não deixou terminar: a rotina do Trabalhador a refaz.

    A linha some quando a amarração conclui. `atencao_em` marca as tentativas esgotadas: o
    /saude/webchat conta essas.
    """

    __tablename__ = "amarracao_webchat"

    id: Mapped[int] = mapped_column(primary_key=True)
    cobranca_id: Mapped[int] = mapped_column(ForeignKey("cobranca.id"), index=True)
    conversa_webchat_id: Mapped[int] = mapped_column(unique=True)
    tentativas: Mapped[int] = mapped_column(default=0)
    proxima_tentativa_em: Mapped[datetime] = mapped_column(index=True)
    atencao_em: Mapped[datetime | None]
    # As notas privadas que já saíram: a tentativa seguinte não as repete.
    nota_webchat_enviada: Mapped[bool] = mapped_column(default=False)
    nota_origem_enviada: Mapped[bool] = mapped_column(default=False)


class CursorCTN(Base):
    """Um marcador de leitura do CTN (ex.: a `coletado_em` da última baixa lida), por nome."""

    __tablename__ = "cursor_ctn"

    nome: Mapped[str] = mapped_column(String(64), primary_key=True)
    valor: Mapped[str] = mapped_column(String(100))


class BaixaCTNRegistro(Base):
    """Uma baixa que o CTN registrou, lida pela rotina das baixas (spec do CTN real, decisão 3).

    O `id_externo` é o `id` da API e torna a leitura idempotente: a baixa que volta no cursor
    inclusivo não é gravada nem aplicada duas vezes. `aplicada_em` nulo = ainda por tratar (a
    conferência não pôde rodar); `efeito` diz o que a rotina fez (`quitou`, `abateu`, `ignorada`,
    `sem_cobranca`, ...). Não é o `BaixaCTN` de `integracoes/ctn.py`, que é o dado em trânsito.
    """

    __tablename__ = "baixa_ctn"

    id: Mapped[int] = mapped_column(primary_key=True)
    id_externo: Mapped[str] = mapped_column(String(64), unique=True)
    matricula: Mapped[str] = mapped_column(String(64), index=True)
    # AAAA/MM, como o CTN a dá.
    mes_ref: Mapped[str] = mapped_column(String(7))
    valor_centavos: Mapped[int]
    forma: Mapped[FormaHistorico] = mapped_column(_enum(FormaHistorico))
    pago_em: Mapped[datetime]
    coletado_em: Mapped[datetime]
    cobranca_id: Mapped[int | None] = mapped_column(ForeignKey("cobranca.id"))
    # A tabela só cresce e a rotina filtra por estes dois a cada 10 minutos (migração 0015).
    aplicada_em: Mapped[datetime | None] = mapped_column(index=True)
    efeito: Mapped[str | None] = mapped_column(String(40), index=True)
