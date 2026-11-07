"""Configuração do serviço, lida das variáveis de ambiente com o prefixo AGENTE_."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agente.integracoes.ctn_eficaz import UNIDADE_PADRAO, URL_EFICAZ
from agente.integracoes.jev_typesafe import MODELO_PADRAO, URL_TYPESAFE
from agente.integracoes.redator import URL_OPENAI
from agente.integracoes.transcritor import MODELO_TRANSCRICAO

_SEGREDO_WEBCHAT_MINIMO = 32

# Por que o Jev real não pode ler conversa de cliente ainda (D12). Vai ao erro do serviço e à
# recusa do `agente reproduzir`.
MENSAGEM_JEV_SEM_LIBERACAO = (
    "AGENTE_JEV_API_KEY está definida, mas AGENTE_JEV_DADOS_REAIS_LIBERADOS não é true: a TypeSafe "
    "ainda não confirmou por escrito Zero Data Retention e No Training (D12), então conversa real "
    "não pode ir ao Jev real. Tire a chave do ambiente para usar o Jev falso, ou libere depois da "
    "confirmação."
)

# Por que o serviço não sobe com o CTN real e o Luna real juntos sem a liberação (M2). Vai ao erro
# do serviço; não leva nenhuma chave.
MENSAGEM_LLM_SEM_LIBERACAO = (
    "AGENTE_CTN_API_KEY e AGENTE_LLM_API_KEY estão definidas, mas AGENTE_LLM_DADOS_REAIS_LIBERADOS "
    "não é true: com o CTN real, o texto das conversas de clientes de verdade iria ao Luna, e a "
    "retenção de dados da OpenAI ainda não foi conferida (T6, D42). Tire uma das duas chaves do "
    "ambiente, ou libere depois da conferência."
)


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
    # Enquanto a retenção da OpenAI não for conferida, o Luna real não lê conversa de cliente do CTN
    # real (M2). Só vale junto com a chave do CTN.
    llm_dados_reais_liberados: bool = False

    @field_validator("llm_dados_reais_liberados", mode="before")
    @classmethod
    def _liberacao_do_llm_vazia_e_fechada(cls, valor):
        return False if isinstance(valor, str) and not valor.strip() else valor


class ConfigJev(ConfigPolitica):
    """O Jev real da TypeSafe (T10), para o serviço, `agente simular` e `agente reproduzir`."""

    # Sem a chave, o serviço usa o JevFalso.
    jev_api_key: str | None = None
    jev_url: str = URL_TYPESAFE
    jev_modelo: str = MODELO_PADRAO
    # Enquanto a TypeSafe não confirmar ZDR e No Training, só texto inventado vai ao Jev real.
    jev_dados_reais_liberados: bool = False

    @field_validator("jev_api_key", mode="before")
    @classmethod
    def _chave_vazia_e_desligada(cls, valor):
        # Sem a quebra de linha ou o espaço que vem da colagem: no cabeçalho, ela dá 401 sem pista.
        return valor.strip() if isinstance(valor, str) and valor.strip() else None

    @field_validator("jev_url", "jev_modelo", mode="before")
    @classmethod
    def _vazio_volta_ao_padrao(cls, valor, info):
        # O .env copiado do .env.example traz a variável vazia.
        padrao = {"jev_url": URL_TYPESAFE, "jev_modelo": MODELO_PADRAO}[info.field_name]
        return valor.strip() if isinstance(valor, str) and valor.strip() else padrao

    @field_validator("jev_dados_reais_liberados", mode="before")
    @classmethod
    def _liberacao_vazia_e_fechada(cls, valor):
        return False if isinstance(valor, str) and not valor.strip() else valor

    @property
    def jev_dados_reais_bloqueados(self) -> bool:
        """Há chave, mas conversa real não foi liberada."""
        return self.jev_api_key is not None and not self.jev_dados_reais_liberados


class ConfigCTN(ConfigPolitica):
    """O CTN real, pela API EFICAZ (T9; só leitura)."""

    # Sem a chave, o serviço usa o CTN falso. A chave não pode aparecer em repr nem em log.
    ctn_api_key: str | None = Field(default=None, repr=False)
    ctn_url: str = URL_EFICAZ
    ctn_unidade: str = UNIDADE_PADRAO

    @field_validator("ctn_api_key", mode="before")
    @classmethod
    def _chave_do_ctn_vazia_e_desligada(cls, valor):
        # Sem a quebra de linha ou o espaço que vem da colagem: no cabeçalho, ela dá 401 sem pista.
        return valor.strip() if isinstance(valor, str) and valor.strip() else None

    @field_validator("ctn_url", "ctn_unidade", mode="before")
    @classmethod
    def _vazio_volta_ao_padrao_do_ctn(cls, valor, info):
        padrao = {"ctn_url": URL_EFICAZ, "ctn_unidade": UNIDADE_PADRAO}[info.field_name]
        return valor.strip() if isinstance(valor, str) and valor.strip() else padrao

    @property
    def ctn_real_ligado(self) -> bool:
        return self.ctn_api_key is not None


class Config(ConfigRedator, ConfigJev, ConfigCTN):
    database_url: str
    modo: Literal["sombra", "ativo"] = "sombra"
    log_level: str = "INFO"
    # Quem conduz o turno (D47): o pipeline do Jev (o padrão) ou uma chamada só ao Luna
    # (experimento).
    turno: Literal["pipeline", "uma_chamada"] = "pipeline"

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

    @field_validator("turno", mode="before")
    @classmethod
    def _turno_vazio_volta_ao_padrao(cls, valor):
        # O .env copiado do .env.example traz a variável vazia.
        return valor.strip() if isinstance(valor, str) and valor.strip() else "pipeline"

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

    @field_validator(
        "chatwoot_conta", "chatwoot_inbox", "chatwoot_time_humano", "webchat_inbox", mode="before"
    )
    @classmethod
    def _numero_vazio_e_ausente(cls, valor):
        # O .env copiado do .env.example traz o número vazio: sem isto o serviço nem sobe.
        return None if isinstance(valor, str) and not valor.strip() else valor

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
