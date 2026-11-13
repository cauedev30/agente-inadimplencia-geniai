"""O que os testes do turno compartilham: a saída do Luna, as dependências e o `Cliente`.

O turno é o de uma chamada ao Luna (D47, D59): os testes dão ao `LLMTurnoFalso` o JSON de cada
chamada e olham o que o orquestrador faz com ele.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agente.db.historico import registrar_mensagem
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixFalso
from agente.tipos import OrigemMensagem
from agente.turno.orquestrador import Dependencias, processar_turno
from agente.turno.uma_chamada import SINAIS

from ..conftest import piloto_com

TELEFONE = "+5583900000001"  # o do cliente da fixture `conversa`
JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)

REPLY_PIX = "Seu valor em aberto é {divida}. Para pagar no Pix, use o código: {pix_copia_e_cola}"
REPLY_LINK = "Seu valor com desconto é {valor_final}. Pague e cadastre o cartão: {link_ctn}"
REPLY_LINK_FINAL = "Fechado! Segue o link com o valor final de {valor_final}: {link_ctn}"
REPLY_NONE = ""


def _bruto(**mudancas) -> dict:
    """O JSON que o Luna devolve: nenhum sinal, tom neutro, INFORM_DEBT com o Pix."""
    bruto = {sinal: False for sinal in SINAIS}
    bruto |= {"tom": "neutro", "acao": "INFORM_DEBT", "reply": REPLY_PIX}
    return bruto | mudancas


def _deps(politica, respostas=(), *, llm=None, ctn=None, **mudancas):
    """As dependências do turno ativo com as falsas; o Luna dá as `respostas`, uma por chamada."""
    return Dependencias(
        politica=piloto_com(politica, TELEFONE),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=llm or LLMTurnoFalso(respostas=list(respostas), registrar=True),
        ctn=ctn or CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)]),
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        **mudancas,
    )


class Cliente:
    """Manda uma mensagem do cliente e roda o turno, um minuto depois do anterior."""

    def __init__(self, sessao, conversa):
        self.sessao, self.conversa, self.n = sessao, conversa, 0

    def __call__(self, texto, deps):
        self.n += 1
        agora = SEGUNDA_10H + timedelta(minutes=self.n)
        registrar_mensagem(
            self.sessao,
            self.conversa.id,
            OrigemMensagem.CLIENTE,
            texto,
            f"cw-{self.n}",
            False,
            agora,
        )
        return processar_turno(self.sessao, self.conversa.id, agora, deps)
