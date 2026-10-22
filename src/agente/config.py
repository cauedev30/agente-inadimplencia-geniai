"""Configuração do serviço, lida das variáveis de ambiente com o prefixo AGENTE_."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTE_", env_file=".env", extra="ignore")

    database_url: str
    modo: Literal["sombra", "ativo"] = "sombra"
    politica_path: Path = Path("politica.toml")
    log_level: str = "INFO"


def carregar_config(**kwargs) -> Config:
    return Config(**kwargs)
