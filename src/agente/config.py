"""Configuração do serviço, lida das variáveis de ambiente com o prefixo AGENTE_."""

from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agente.integracoes.ctn_eficaz import UNIDADE_PADRAO, URL_EFICAZ
from agente.integracoes.llm_turno import URL_OPENAI
from agente.integracoes.transcritor import MODELO_TRANSCRICAO


def mensagem_llm_sem_liberacao(fontes: Sequence[str], destinos: Sequence[str]) -> str:
    """Por que o serviço não sobe com dado real de cliente indo à OpenAI sem a liberação (M2).

    Vai ao erro do serviço; só nomeia as variáveis que dispararam, nunca os valores.
    """
    return (
        f"{' e '.join(fontes)} (dado real de cliente) e {' e '.join(destinos)} (a OpenAI recebe o "
        "texto ou o áudio do cliente) estão definidas, mas AGENTE_LLM_DADOS_REAIS_LIBERADOS não é "
        "true: a retenção de dados da OpenAI ainda não foi conferida (T6, D42). Tire as variáveis "
        "de uma das pontas do ambiente, ou libere depois da conferência."
    )


class ConfigPolitica(BaseSettings):
    """Só o caminho da política, para os comandos que não usam o banco (`agente simular`)."""

    # O erro de configuração diz o nome do campo, nunca o valor: as chaves não vão ao log da subida.
    model_config = SettingsConfigDict(
        env_prefix="AGENTE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    politica_path: Path = Path("politica.toml")


class ConfigRedator(ConfigPolitica):
    """O Luna do turno de uma chamada (D42, D59), para o serviço."""

    # Sem a chave, o serviço não sobe.
    llm_api_key: str | None = None
    llm_url: str = URL_OPENAI
    llm_modelo: str = "gpt-6-luna"
    # Entra no corpo de todo pedido. O Luna recusa a temperatura (HTTP 400) sem o reasoning_effort
    # "none". Em variável de ambiente, vai como JSON.
    llm_corpo_extra: dict = {"reasoning_effort": "none"}
    # Enquanto a retenção da OpenAI não for conferida, nada de cliente de verdade (CTN real,
    # Chatwoot real) vai à OpenAI, nem ao Luna nem à transcrição (M2).
    llm_dados_reais_liberados: bool = False

    @field_validator("llm_dados_reais_liberados", mode="before")
    @classmethod
    def _liberacao_do_llm_vazia_e_fechada(cls, valor):
        return False if isinstance(valor, str) and not valor.strip() else valor


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


class Config(ConfigRedator, ConfigCTN):
    database_url: str
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

    @field_validator(
        "webhook_segredo",
        "transcricao_url",
        "transcricao_api_key",
        "transcricao_modelo",
        "pix_webhook_segredo",
        "ctn_webhook_segredo",
    )
    @classmethod
    def _segredo_vazio_e_desligado(cls, valor: str | None) -> str | None:
        # O .env copiado do .env.example traz a variável vazia: um HMAC de chave vazia é calculável
        # por qualquer um, então o vazio vale como sem segredo.
        return valor if valor and valor.strip() else None

    @field_validator("chatwoot_conta", "chatwoot_inbox", "chatwoot_time_humano", mode="before")
    @classmethod
    def _numero_vazio_e_ausente(cls, valor):
        # O .env copiado do .env.example traz o número vazio: sem isto o serviço nem sobe.
        return None if isinstance(valor, str) and not valor.strip() else valor

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
    def chatwoot_real_ligado(self) -> bool:
        return bool(self.chatwoot_url and self.chatwoot_conta and self.chatwoot_token)

    @property
    def fontes_de_dado_real(self) -> list[str]:
        """As variáveis que trazem conversa ou carteira de cliente de verdade ao serviço."""
        return [
            variavel
            for variavel, ligada in (
                ("AGENTE_CTN_API_KEY", self.ctn_real_ligado),
                ("AGENTE_CHATWOOT_*", self.chatwoot_real_ligado),
            )
            if ligada
        ]

    @property
    def destinos_externos_do_cliente(self) -> list[str]:
        """As variáveis que mandam texto ou áudio do cliente à OpenAI (o Luna e a transcrição)."""
        return [
            variavel
            for variavel, ligado in (
                ("AGENTE_LLM_API_KEY", bool(self.llm_api_key)),
                ("AGENTE_TRANSCRICAO_*", self.transcricao_ligada),
            )
            if ligado
        ]

    @property
    def transcricao_ligada(self) -> bool:
        return self.transcricao_url is not None and self.transcricao_api_key is not None

    @property
    def transcricao_modelo_usado(self) -> str:
        return self.transcricao_modelo or MODELO_TRANSCRICAO


def carregar_config(**kwargs) -> Config:
    return Config(**kwargs)
