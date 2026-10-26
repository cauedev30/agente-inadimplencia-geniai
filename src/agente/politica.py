"""Carrega e valida o politica.toml: todos os parâmetros de negócio do agente."""

import tomllib
from datetime import date, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agente.dominio.telefone import normalizar_telefone


class Regua(StrEnum):
    NR = "NR"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    PREVIA = "PREVIA"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"


DiaSemana = Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
Limiar = Annotated[float, Field(ge=0, le=1)]


class _Imutavel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _em_e164(telefones: frozenset[str]) -> frozenset[str]:
    """Os telefones em E.164, como a carteira e o webhook os guardam; o inválido recusa."""
    normalizados = {normalizar_telefone(telefone) for telefone in telefones}
    if None in normalizados:
        raise ValueError("telefone inválido: use o DDD e o número, com ou sem +55")
    return frozenset(normalizados)


class ReguaParametros(_Imutavel):
    desconto_max_centavos: int = Field(ge=0)
    n_degraus: int = Field(ge=1)


class Horario(_Imutavel):
    inicio: time
    fim: time

    @model_validator(mode="before")
    @classmethod
    def _de_lista(cls, valor):
        if isinstance(valor, list | tuple) and len(valor) == 2:
            return {"inicio": valor[0], "fim": valor[1]}
        return valor

    @model_validator(mode="after")
    def _inicio_antes_do_fim(self):
        if self.inicio >= self.fim:
            raise ValueError("o início tem de ser antes do fim")
        return self


class Janela(_Imutavel):
    fuso: str
    dias: dict[DiaSemana, Horario]
    feriados: frozenset[date] = frozenset()

    @field_validator("fuso")
    @classmethod
    def _fuso_existe(cls, valor: str) -> str:
        try:
            ZoneInfo(valor)
        except (ZoneInfoNotFoundError, ValueError) as erro:
            raise ValueError(f"fuso desconhecido: {valor}") from erro
        return valor

    @property
    def zona(self) -> ZoneInfo:
        return ZoneInfo(self.fuso)


class Propensao(_Imutavel):
    n_meses: int = Field(ge=1)
    tolerancia_dias: int = Field(ge=0)


class ColunasCarteira(_Imutavel):
    """O nome, no CSV, de cada coluna que o agente lê. A coluna da régua é opcional."""

    matricula: str = "matricula"
    nome: str = "nome"
    telefone: str = "telefone"
    mensalidades_abertas: str = "mensalidades_abertas"
    regua: str = "regua"


class Carteira(_Imutavel):
    separador: str = Field(default=";", min_length=1, max_length=1)
    colunas: ColunasCarteira = ColunasCarteira()


class LiberacaoSombra(_Imutavel):
    modo: Literal["sombra"] = "sombra"


class LiberacaoPiloto(_Imutavel):
    """Só os telefones da lista ficam no modo ativo: o piloto com a equipe (T7)."""

    modo: Literal["piloto"]
    telefones: frozenset[str] = Field(min_length=1)

    _normalizar = field_validator("telefones")(_em_e164)


class LiberacaoPercentual(_Imutavel):
    """Uma parte da carteira no modo ativo; 100 é a carteira toda (T7)."""

    modo: Literal["percentual"]
    percentual: int = Field(ge=0, le=100)


Liberacao = Annotated[
    LiberacaoSombra | LiberacaoPiloto | LiberacaoPercentual, Field(discriminator="modo")
]


class Politica(_Imutavel):
    mensalidade_centavos: int = Field(gt=0)
    teto_resolucao: int = Field(ge=1)
    regua_acima_de_6: Regua
    pix_validade_horas: int = Field(ge=1)
    # O Pix pendente que vence em menos que isto não é reaproveitado no pedido seguinte do cliente.
    pix_reaproveitar_minimo_minutos: int = Field(ge=0)
    # O handoff expira o Pix pendente? Padrão não: o Pix continua valendo e o pagamento conta.
    handoff_expira_pix: bool = False
    objecoes_sem_desconto_ate_humano: int = Field(ge=1)
    confianca_minima: Limiar
    rajada_segundos: int = Field(ge=0)
    audio_max_segundos: int = Field(ge=1)
    # O download do áudio do cliente (D43): o maior arquivo, o prazo de tudo e, para o formato cuja
    # duração não se lê (qualquer um fora do Ogg Opus), o maior arquivo que vale pelos 120 s.
    audio_max_bytes: int = Field(ge=1)
    audio_prazo_segundos: int = Field(ge=1)
    audio_sem_duracao_max_bytes: int = Field(ge=1)
    # A chamada de transcrição: o prazo e o tamanho do texto guardado (o resto é cortado).
    transcricao_prazo_segundos: int = Field(ge=1)
    transcricao_max_caracteres: int = Field(ge=2)
    # A nota privada com o texto do áudio sai também em sombra: o atendente responde nessa fase e a
    # nota o poupa de ouvir o áudio. No modo ativo, sempre sai.
    nota_transcricao_em_sombra: bool
    # Os pedidos de texto, quando a rajada fica sem nada legível (só áudio que não foi lido; D43).
    pedir_texto_audio_longo: str = Field(min_length=1)
    pedir_texto_audio_falhou: str = Field(min_length=1)
    jev_timeout_ms: int = Field(ge=1)
    integracao_tentativas: int = Field(ge=1)
    erro_interno_tentativas: int = Field(ge=1)
    integracao_espera_segundos: int = Field(ge=1)
    integracao_espera_maxima_segundos: int = Field(ge=1)
    link_canal_externo: str
    webchat_token_validade_horas: int = Field(ge=1)
    webchat_cpf_digitos: int = Field(ge=1, le=11)
    webchat_cpf_tentativas: int = Field(ge=1)
    # O bloqueio depois dos erros no CPF dura isto; então as tentativas zeram.
    webchat_cpf_bloqueio_horas: int = Field(ge=1)
    # A amarração só vale para a cobrança que passou pelo CPF há menos que isto (defesa contra o
    # `identifier` forjado no widget).
    webchat_verificacao_validade_minutos: int = Field(ge=1)
    template_abertura: str = Field(min_length=1)
    template_retomada: str = Field(min_length=1)
    template_confirmacao: str = Field(min_length=1)
    # true: o agente registra no CTN a baixa do pagamento pelo link (o link não baixa, T9.6).
    # false: o link já baixa no CTN e o agente não dá a segunda baixa. A do Pix é sempre do agente.
    agente_da_baixa_no_link: bool
    anexos_aceitos: frozenset[str]
    comando_reinicio: str = Field(min_length=1)
    telefones_equipe: frozenset[str] = frozenset()
    reguas: dict[Regua, ReguaParametros]
    janela: Janela
    propensao: Propensao
    limiares_jev1: dict[str, Limiar]
    limiares_jev2: dict[str, Limiar]
    carteira: Carteira = Carteira()
    liberacao: Liberacao = LiberacaoSombra()

    @field_validator("liberacao", mode="before")
    @classmethod
    def _liberacao_em_texto(cls, valor):
        # `liberacao = "sombra"` é o atalho de `liberacao = { modo = "sombra" }`.
        return {"modo": valor} if isinstance(valor, str) else valor

    _normalizar_equipe = field_validator("telefones_equipe")(_em_e164)

    @model_validator(mode="after")
    def _espera_maxima_nao_menor_que_a_inicial(self):
        if self.integracao_espera_maxima_segundos < self.integracao_espera_segundos:
            raise ValueError("integracao_espera_maxima_segundos: menor que a espera inicial")
        return self

    def espera_integracao(self, falhas: int) -> timedelta:
        """A espera depois da `falhas`-ésima falha seguida: dobra a cada uma, até o teto."""
        segundos = self.integracao_espera_segundos * 2 ** max(falhas - 1, 0)
        return timedelta(seconds=min(segundos, self.integracao_espera_maxima_segundos))

    @field_validator("reguas")
    @classmethod
    def _todas_as_reguas(cls, valor: dict[Regua, ReguaParametros]):
        faltando = [r.value for r in Regua if r not in valor]
        if faltando:
            raise ValueError(f"faltam as réguas: {', '.join(faltando)}")
        return valor


class PoliticaInvalida(ValueError):
    """O politica.toml tem um campo errado; a mensagem diz qual."""


def carregar_politica(caminho: Path) -> Politica:
    with open(caminho, "rb") as arquivo:
        dados = tomllib.load(arquivo)
    try:
        return Politica.model_validate(dados)
    except ValidationError as erro:
        campos = "; ".join(
            f"{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}" for e in erro.errors()
        )
        raise PoliticaInvalida(f"{caminho}: {campos}") from erro
