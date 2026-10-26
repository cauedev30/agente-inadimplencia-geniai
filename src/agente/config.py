"""Configuração do serviço, lida das variáveis de ambiente com o prefixo AGENTE_."""

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agente.integracoes.redator import URL_OPENAI
from agente.integracoes.transcritor import MODELO_TRANSCRICAO

_SEGREDO_WEBCHAT_MINIMO = 32


class ConfigPolitica(BaseSettings):
    """Só o caminho da política, para os comandos que não usam o banco (`agente simular`)."""

    # O erro de configuração diz o nome do campo, nunca o valor: as chaves não vão ao log da subida.
    model_config = SettingsConfigDict(
        env_prefix="AGENTE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    politica_path: Path = Path("politica.toml")


class ConfigRedator(ConfigPolitica):
    """O LLM que redige (D42, T6), para o serviço e o `agente comparar-redatores`."""

    # Sem a chave, o serviço usa o RedatorFalso.
    llm_api_key: str | None = None
    llm_url: str = URL_OPENAI
    llm_modelo: str = "gpt-6-luna"
    # Entra no corpo de todo pedido. O Luna recusa a temperatura (HTTP 400) sem o reasoning_effort
    # "none". Em variável de ambiente, vai como JSON.
    llm_corpo_extra: dict = {"reasoning_effort": "none"}


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

    # Transcrição do áudio do cliente (D43, T11), com variáveis próprias, separadas das do LLM.
    # Sem a URL e a chave, ela fica desligada e o áudio cai no pedido de texto. Só uma das duas é
    # erro.
    transcricao_url: str | None = None
    transcricao_api_key: str | None = None
    transcricao_modelo: str | None = None

    # Webhooks de pagamento (T1 e T9): sem o segredo, o webhook fica desligado.
    pix_webhook_segredo: str | None = None
    ctn_webhook_segredo: str | None = None

    # Webchat (7.1, 7.2): sem o segredo do token ou a chave da identidade, a página fica desligada.
    webchat_segredo: str | None = None
    webchat_identidade_hmac: str | None = None  # o token HMAC da inbox Website do Chatwoot
    webchat_website_token: str | None = None  # o token público da inbox Website (vai ao navegador)
    webchat_inbox: int | None = None  # a inbox Website, para reconhecer a conversa do webchat (7.3)
    webchat_url_base: str | None = None  # o domínio do link da 6ª mensagem (G7)

    @field_validator(
        "webhook_segredo",
        "transcricao_url",
        "transcricao_api_key",
        "transcricao_modelo",
        "pix_webhook_segredo",
        "ctn_webhook_segredo",
        "webchat_segredo",
        "webchat_identidade_hmac",
        "webchat_website_token",
        "webchat_url_base",
    )
    @classmethod
    def _segredo_vazio_e_desligado(cls, valor: str | None) -> str | None:
        # O .env copiado do .env.example traz a variável vazia: um HMAC de chave vazia é calculável
        # por qualquer um, então o vazio vale como sem segredo.
        return valor if valor and valor.strip() else None

    @field_validator("webchat_segredo")
    @classmethod
    def _segredo_do_webchat_longo(cls, valor: str | None) -> str | None:
        # Com um token na mão, um segredo curto se quebra offline e deixa forjar o de qualquer
        # cobrança.
        if valor is not None and len(valor) < _SEGREDO_WEBCHAT_MINIMO:
            raise ValueError(
                f"o segredo do webchat precisa de ao menos {_SEGREDO_WEBCHAT_MINIMO} caracteres"
            )
        return valor

    @model_validator(mode="after")
    def _transcricao_inteira_ou_nenhuma(self):
        faltam = [
            variavel
            for variavel, valor in (
                ("AGENTE_TRANSCRICAO_URL", self.transcricao_url),
                ("AGENTE_TRANSCRICAO_API_KEY", self.transcricao_api_key),
            )
            if valor is None
        ]
        so_o_modelo = self.transcricao_modelo is not None and len(faltam) == 2
        if faltam and (len(faltam) == 1 or so_o_modelo):
            raise ValueError(f"transcrição configurada pela metade: faltam {', '.join(faltam)}")
        return self

    @property
    def transcricao_ligada(self) -> bool:
        return self.transcricao_url is not None and self.transcricao_api_key is not None

    @property
    def transcricao_modelo_usado(self) -> str:
        return self.transcricao_modelo or MODELO_TRANSCRICAO

    @property
    def webchat_ligado(self) -> bool:
        """A página /atendimento e o link da 6ª mensagem só valem com tudo isto presente."""
        return bool(
            self.webchat_segredo
            and self.webchat_identidade_hmac
            and self.webchat_website_token
            and self.chatwoot_url
        )


def carregar_config(**kwargs) -> Config:
    return Config(**kwargs)
