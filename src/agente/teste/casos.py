"""Os casos do modo teste: quem é testador, a trava de subida e a ativação de um caso (D56)."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.db.modelos import Cliente, Cobranca
from agente.dominio.telefone import variantes_telefone
from agente.politica import LiberacaoPiloto, Politica
from agente.tipos import StatusCobranca


def do_piloto(telefone: str, politica: Politica) -> bool:
    """O telefone é de um testador da lista do piloto, com ou sem o 9 (A1)."""
    if not isinstance(politica.liberacao, LiberacaoPiloto):
        return False
    return bool(variantes_telefone(telefone) & politica.liberacao.telefones)


def exigir_piloto_fora_da_carteira(sessao: Session, politica: Politica) -> None:
    """A trava de subida: o testador do piloto não pode ter cobrança real ATIVA.

    Os casos do teste trocam a carteira do testador pelos inventados; uma cobrança real dele (da
    importação) cobraria a pessoa de verdade e misturaria as duas. Só roda com o modo teste; sem
    ele o serviço sobe como sempre. A mensagem não leva telefone nem nome, só a contagem.
    """
    teste = politica.teste
    if teste is None or not isinstance(politica.liberacao, LiberacaoPiloto):
        return
    telefones: set[str] = set()
    for telefone in politica.liberacao.telefones:
        telefones |= variantes_telefone(telefone)
    reais = sessao.scalar(
        select(func.count())
        .select_from(Cobranca)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(
            Cliente.telefone.in_(telefones),
            Cobranca.status == StatusCobranca.ATIVA,
            Cobranca.campanha != teste.campanha,
        )
    )
    if reais:
        raise ValueError(
            f"o modo teste não sobe: {reais} cobrança(s) ATIVA(s) fora da campanha "
            f"'{teste.campanha}' em telefone(s) do piloto. Encerre-as ou tire o telefone do piloto"
        )
