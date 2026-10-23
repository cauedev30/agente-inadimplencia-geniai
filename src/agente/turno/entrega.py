"""A mensagem gravada PENDENTE que vai ao Chatwoot e vira ENVIADA, sem sair duas vezes.

O turno (tarefa 5.3) e a confirmação do pagamento (tarefa 6.1) gravam a intenção de enviar antes
de mandar. Se a tentativa anterior entregou e perdeu a resposta, `entregue` acha a mensagem no
Chatwoot.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Conversa, Mensagem
from agente.integracoes.chatwoot import Chatwoot
from agente.tipos import EstadoEnvio


def _normalizar(texto: str) -> str:
    return " ".join(texto.split())


def entregue(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, chatwoot: Chatwoot
) -> str | None:
    """O id da mensagem no Chatwoot, se ela já foi entregue (a resposta do envio se perdeu).

    Só vale a de mesmo texto que o banco ainda não conhece nesta conversa: o mesmo texto já saiu
    em outro turno. O texto é comparado sem espaços a mais nem diferença de quebra de linha, que
    o canal pode mexer.
    """
    conhecidas = set(
        sessao.scalars(
            select(Mensagem.chatwoot_message_id).where(
                Mensagem.conversa_id == conversa.id, Mensagem.chatwoot_message_id.is_not(None)
            )
        )
    )
    procurado = _normalizar(mensagem.texto)
    for chatwoot_id, texto in chatwoot.mensagens_do_agente(conversa.chatwoot_conversa_id):
        if chatwoot_id not in conhecidas and _normalizar(texto) == procurado:
            return chatwoot_id
    return None


def confirmar_envio(sessao: Session, mensagem: Mensagem, chatwoot_id: str) -> None:
    mensagem.chatwoot_message_id = chatwoot_id
    mensagem.envio = EstadoEnvio.ENVIADA
    sessao.flush()
