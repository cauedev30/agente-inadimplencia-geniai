"""O link do webchat que a 6ª mensagem leva (tarefa 7.4)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from agente.politica import Politica
from agente.webchat.token import emitir_token


@dataclass(frozen=True)
class ConfigWebchat:
    """O que o link precisa: o domínio da página (G7) e o segredo do token."""

    url_base: str
    segredo: str


def link_do_webchat(
    webchat: ConfigWebchat, politica: Politica, cobranca_id: int, agora: datetime
) -> str:
    """`<url_base>/atendimento/<token>`, com o token da cobrança e a validade do politica.toml."""
    expira_em = agora + timedelta(hours=politica.webchat_token_validade_horas)
    token = emitir_token(webchat.segredo, cobranca_id, expira_em)
    return f"{webchat.url_base.rstrip('/')}/atendimento/{token}"


def link_canal_externo(
    webchat: ConfigWebchat | None, politica: Politica, cobranca_id: int, agora: datetime
) -> str:
    """O link do botão da 6ª mensagem: o do webchat, ou o provisório quando não há domínio (G7)."""
    if webchat is None:
        return politica.link_canal_externo
    return link_do_webchat(webchat, politica, cobranca_id, agora)
