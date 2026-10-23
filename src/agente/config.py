"""Configuração do serviço, lida das variáveis de ambiente com o prefixo AGENTE_."""

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigPolitica(BaseSettings):
    """Só o caminho da política, para os comandos que não usam o banco (`agente simular`)."""

    model_config = SettingsConfigDict(env_prefix="AGENTE_", env_file=".env", extra="ignore")

    politica_path: Path = Path("politica.toml")


class ConfigRedator(ConfigPolitica):
    """O LLM que redige (T6), para o serviço e o `agente comparar-redatores`."""

    # Sem a chave, o serviço usa o RedatorFalso.
    groq_api_key: str | None = None
    groq_modelo: str = "openai/gpt-oss-120b"  # o candidato da D6


class Config(ConfigRedator):
    database_url: str
    modo: Literal["sombra", "ativo"] = "sombra"
    log_level: str = "INFO"

    # Chatwoot (T3). Vazios até a conta, a inbox e o token do Agent Bot existirem.
    chatwoot_url: str | None = None
    chatwoot_conta: int | None = None
    chatwoot_inbox: int | None = None
    chatwoot_token: str | None = None
    chatwoot_time_humano: int | None = None
    webhook_segredo: str | None = None

    # Webhooks de pagamento (T1 e T9): sem o segredo, o webhook fica desligado.
    pix_webhook_segredo: str | None = None
    ctn_webhook_segredo: str | None = None

    @field_validator("webhook_segredo", "pix_webhook_segredo", "ctn_webhook_segredo")
    @classmethod
    def _segredo_vazio_e_desligado(cls, valor: str | None) -> str | None:
        # O .env copiado do .env.example traz a variável vazia: um HMAC de chave vazia é calculável
        # por qualquer um, então o vazio vale como sem segredo.
        return valor if valor and valor.strip() else None


def carregar_config(**kwargs) -> Config:
    return Config(**kwargs)
