"""Saída para o humano. Toda saída do robô passa por aqui: leva ao canal externo (D5) ou, por
desconfiança de golpe, fica no próprio WhatsApp (D44)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import Conversa, Transicao
from agente.integracoes.chatwoot import Chatwoot
from agente.pagamento.expirar import expirar_pendentes
from agente.politica import Politica
from agente.tipos import EstadoConversa, Laudo, MotivoHumano, OrigemMensagem, TipoFixa
from agente.turno.catalogo import DadosFixa, mensagem_fixa
from agente.webchat.link import ConfigWebchat, link_canal_externo


def _nota(motivo: MotivoHumano, laudos: list[Laudo]) -> str:
    linhas = [f"Saída para o humano: {motivo.value}"]
    linhas += [
        f"- {laudo.pergunta}: {laudo.valor} (p={laudo.probabilidade:.2f}, c={laudo.confianca:.2f})"
        for laudo in laudos
    ]
    return "\n".join(linhas)


def enviar_fixa(
    sessao: Session,
    chatwoot: Chatwoot,
    conversa: Conversa,
    tipo: TipoFixa,
    dados: DadosFixa,
    agora: datetime,
) -> None:
    """Envia uma mensagem fixa e a grava fora do teto."""
    mensagem = mensagem_fixa(tipo, dados)
    chatwoot_id = chatwoot.enviar_mensagem(
        conversa.chatwoot_conversa_id, mensagem.texto, mensagem.botoes
    )
    registrar_mensagem(
        sessao, conversa.id, OrigemMensagem.FIXA, mensagem.texto, chatwoot_id, False, agora
    )


def fazer_handoff(
    sessao: Session,
    chatwoot: Chatwoot,
    conversa: Conversa,
    motivo: MotivoHumano,
    laudos: list[Laudo],
    politica: Politica,
    time_id: int | None,
    agora: datetime,
    laudo_id: int | None = None,
    webchat: ConfigWebchat | None = None,
) -> None:
    """Nota com o laudo, etiqueta, atribuição, status aberta, estado e a mensagem do canal.

    A mensagem do canal leva o link do webchat com o token da cobrança; sem `webchat` (sem o
    domínio, G7), leva o link provisório do politica.toml. Os motivos de
    `motivos_humano_no_whatsapp` (D44) mandam a fixa sem link, e nenhum token é gerado.
    """
    if conversa.chatwoot_conversa_id is None:
        raise ValueError(f"a conversa {conversa.id} não está ligada a uma conversa do Chatwoot")
    chatwoot_id = conversa.chatwoot_conversa_id
    chatwoot.nota_privada(chatwoot_id, _nota(motivo, laudos))
    chatwoot.etiquetar(chatwoot_id, [motivo.value.lower()])
    if time_id is not None:
        chatwoot.atribuir(chatwoot_id, time_id)
    chatwoot.mudar_status(chatwoot_id, "open")
    transicionar(
        sessao, conversa, EstadoConversa.ATENDIMENTO_HUMANO, motivo, laudo_id, momento=agora
    )
    if politica.handoff_expira_pix:
        expirar_pendentes(sessao, conversa.cobranca)
    nome = conversa.cobranca.cliente.nome
    if motivo in politica.motivos_humano_no_whatsapp:
        # D44: o atendente segue na mesma conversa; sem link, e sem gerar o token do webchat.
        dados = DadosFixa(
            nome=nome,
            valor_centavos=None,
            link_cartao=None,
            link_canal_externo="",
            texto=politica.texto_humano_no_whatsapp,
        )
        enviar_fixa(sessao, chatwoot, conversa, TipoFixa.HUMANO_NO_WHATSAPP, dados, agora)
        return
    dados = DadosFixa(
        nome=nome,
        valor_centavos=None,
        link_cartao=None,
        link_canal_externo=link_canal_externo(webchat, politica, conversa.cobranca_id, agora),
    )
    enviar_fixa(sessao, chatwoot, conversa, TipoFixa.CANAL_EXTERNO, dados, agora)


def devolver_ao_robo(sessao: Session, conversa: Conversa, agora: datetime) -> None:
    """O atendente marcou a conversa como Pendente: o robô reassume no estado de antes."""
    ultima_saida = sessao.scalar(
        select(Transicao)
        .where(
            Transicao.conversa_id == conversa.id,
            Transicao.para == EstadoConversa.ATENDIMENTO_HUMANO,
        )
        .order_by(Transicao.id.desc())
    )
    anterior = ultima_saida.de if ultima_saida is not None else EstadoConversa.ENGAJADO
    conversa.human_owned = False
    transicionar(sessao, conversa, anterior, None, None, momento=agora)
