"""O que os testes do modo teste compartilham: o Chatwoot do testador, as dependências e o
`Mandante`, que manda mensagens do cliente e roda o turno."""

from datetime import timedelta

from agente.db.historico import registrar_mensagem
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import CTNComposto, CTNFalso
from agente.integracoes.ctn_teste import CTNEscritaDeTeste, RoteadorCTN
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixDeTeste
from agente.tipos import OrigemMensagem
from agente.turno.orquestrador import Dependencias, processar_turno
from tests.turno.auxiliares import SEGUNDA_10H, _bruto

CHATWOOT_ID = 555


def montar_deps(politica, casos, chatwoot=None, *, com_casos=True):
    ctn = CTNComposto(RoteadorCTN(casos, CTNFalso()), CTNEscritaDeTeste())
    return Dependencias(
        politica=politica,
        chatwoot=chatwoot or ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=LLMTurnoFalso(respostas=[_bruto()] * 20, registrar=True),
        ctn=ctn,
        pix=PixDeTeste(agora=lambda: SEGUNDA_10H),
        conferir_no_contato=True,
        casos=casos if com_casos else None,
    )


class Mandante:
    """Manda mensagens do cliente e roda o turno, um minuto depois da anterior."""

    def __init__(self, sessao, conversa, inicio=SEGUNDA_10H):
        self.sessao, self.conversa, self.n, self.inicio = sessao, conversa, 0, inicio

    def manda(self, *textos):
        for texto in textos:
            self.n += 1
            registrar_mensagem(
                self.sessao,
                self.conversa.id,
                OrigemMensagem.CLIENTE,
                texto,
                f"cw-{self.conversa.id}-{self.n}",
                False,
                self.inicio + timedelta(minutes=self.n),
            )
        self.sessao.commit()

    def agora(self):
        return self.inicio + timedelta(minutes=self.n)

    def roda(self, deps):
        return processar_turno(self.sessao, self.conversa.id, self.agora(), deps)
