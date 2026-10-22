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

    # Chatwoot (T3). Vazios até a conta, a inbox e o token do Agent Bot existirem.
    chatwoot_url: str | None = None
    chatwoot_conta: int | None = None
    chatwoot_inbox: int | None = None
    chatwoot_token: str | None = None
    chatwoot_time_humano: int | None = None
    webhook_segredo: str | None = None


def carregar_config(**kwargs) -> Config:
    return Config(**kwargs)
