"""Carrega e valida o politica.toml: todos os parâmetros de negócio do agente."""

import tomllib
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


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


class Politica(_Imutavel):
    mensalidade_centavos: int = Field(gt=0)
    teto_resolucao: int = Field(ge=1)
    regua_acima_de_6: Regua
    pix_validade_horas: int = Field(ge=1)
    objecoes_sem_desconto_ate_humano: int = Field(ge=1)
    confianca_minima: Limiar
    rajada_segundos: int = Field(ge=0)
    audio_max_segundos: int = Field(ge=1)
    jev_timeout_ms: int = Field(ge=1)
    link_canal_externo: str
    reguas: dict[Regua, ReguaParametros]
    janela: Janela
    propensao: Propensao
    limiares_jev1: dict[str, Limiar]
    limiares_jev2: dict[str, Limiar]
    carteira: Carteira = Carteira()

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
