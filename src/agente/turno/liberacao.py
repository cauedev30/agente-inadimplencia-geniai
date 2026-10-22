"""Liberação gradual do modo ativo (tarefa 5.5, T7): sombra, piloto, uma parte e a carteira toda.

Mudar a liberação é só mudar o `liberacao` do politica.toml. O percentual sorteia pela cobrança, com
um hash do id, nunca ao acaso: a mesma cobrança cai sempre do mesmo lado, e subir o percentual não
devolve ninguém para a sombra.
"""

import hashlib

from agente.db.modelos import Cobranca
from agente.politica import LiberacaoPercentual, LiberacaoPiloto, Politica
from agente.tipos import Modo

CARTEIRA_TODA = LiberacaoPercentual(modo="percentual", percentual=100)


def _faixa(cobranca_id: int) -> int:
    """De 0 a 99, fixa para cada cobrança."""
    resumo = hashlib.sha256(f"cobranca:{cobranca_id}".encode()).digest()
    return int.from_bytes(resumo[:8], "big") % 100


def modo_da_conversa(cobranca: Cobranca, politica: Politica) -> Modo:
    liberacao = politica.liberacao
    if isinstance(liberacao, LiberacaoPiloto):
        ativo = cobranca.cliente.telefone in liberacao.telefones
    elif isinstance(liberacao, LiberacaoPercentual):
        ativo = _faixa(cobranca.id) < liberacao.percentual
    else:
        ativo = False
    return Modo.ATIVO if ativo else Modo.SOMBRA


def liberar_tudo(politica: Politica) -> Politica:
    """A mesma política com a carteira toda no modo ativo (simulação e testes)."""
    return politica.model_copy(update={"liberacao": CARTEIRA_TODA})
