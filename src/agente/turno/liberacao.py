"""Quem conversa com o agente (D61): só os telefones do piloto.

Mudar o piloto é só mudar o `liberacao` do politica.toml. Quem está fora é ignorado: sem Luna, sem
CTN e sem mensagem. O piloto vazio (o padrão) não conversa com ninguém.
"""

from agente.dominio.telefone import variantes_telefone
from agente.politica import Politica


def no_piloto(telefone: str, politica: Politica) -> bool:
    """O telefone está no piloto, com ou sem o 9 do celular (A1)."""
    return not variantes_telefone(telefone).isdisjoint(politica.liberacao.telefones)
