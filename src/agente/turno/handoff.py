"""Saída para o humano. Toda saída do robô passa por aqui: avisa que o atendente segue na
mesma conversa (D60) ou, por desconfiança de golpe, manda a fixa do WhatsApp (D44)."""

from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem, transicionar
from agente.db.modelos import Conversa, Transicao
from agente.integracoes.chatwoot import Chatwoot
from agente.pagamento.expirar import expirar_pendentes
from agente.politica import Politica
from agente.tipos import EstadoConversa, Laudo, MotivoHumano, OrigemMensagem, TipoFixa
from agente.turno.catalogo import DadosFixa, mensagem_fixa

NOTA_TETO_REPETIDO = "O teto já foi atingido; o cliente escreveu de novo."
NOTA_CONCILIACAO_REPETIDA = (
    "A conferência no CTN segue mostrando a cobrança quitada; o cliente escreveu de novo."
)
NOTA_REPETIDA = "A conversa já saiu por este motivo; o cliente escreveu de novo."
NOTA_FORA_DAS_24H = (
    "O cliente escreveu fora do horário e passaram as 24h do WhatsApp: "
    "só dá para responder com template"
)


def _nota(motivo: MotivoHumano, laudos: list[Laudo], *, repetido: bool = False) -> str:
    linhas = [f"Saída para o humano: {motivo.value}"]
    if motivo is MotivoHumano.FORA_DAS_24H:
        linhas.append(NOTA_FORA_DAS_24H)
    elif repetido:
        linhas.append(
            {
                MotivoHumano.TETO: NOTA_TETO_REPETIDO,
                MotivoHumano.CONCILIACAO: NOTA_CONCILIACAO_REPETIDA,
            }.get(motivo, NOTA_REPETIDA)
        )
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


def _saiu_para_o_humano(sessao: Session, conversa: Conversa, *motivos: MotivoHumano) -> bool:
    consulta = select(
        exists().where(
            Transicao.conversa_id == conversa.id,
            Transicao.para == EstadoConversa.ATENDIMENTO_HUMANO,
            Transicao.motivo.in_(motivos),
        )
    )
    return bool(sessao.scalar(consulta))


def ja_saiu_pelo_teto(sessao: Session, conversa: Conversa) -> bool:
    """A conversa já recebeu a 6ª mensagem e saiu para o humano pelo teto (D33)."""
    return _saiu_para_o_humano(sessao, conversa, MotivoHumano.TETO)


def ja_saiu_por_conciliacao(sessao: Session, conversa: Conversa) -> bool:
    """A conversa já saiu para o humano conferir o pagamento (CONCILIACAO): a cobrança quitada no
    CTN só leva essa mensagem uma vez."""
    return _saiu_para_o_humano(sessao, conversa, MotivoHumano.CONCILIACAO)


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
    *,
    enviar_mensagem: bool = True,
) -> None:
    """Nota com o laudo, etiqueta, atribuição, status aberta, estado e a mensagem do canal.

    O atendente segue na mesma conversa do WhatsApp (D60): a mensagem é a `CANAL_EXTERNO`, sem botão
    e sem link; por DESCONFIANCA (golpe, D44), a fixa `HUMANO_NO_WHATSAPP`. Sem `enviar_mensagem`,
    o cliente não recebe nada: é a volta ao humano de quem já recebeu a 6ª, ou de quem já recebeu a
    mensagem da cobrança quitada (CONCILIACAO). O texto ao cliente é sempre fixo: o Luna não
    redige o handoff.
    """
    if conversa.chatwoot_conversa_id is None:
        raise ValueError(f"a conversa {conversa.id} não está ligada a uma conversa do Chatwoot")
    chatwoot_id = conversa.chatwoot_conversa_id
    chatwoot.nota_privada(chatwoot_id, _nota(motivo, laudos, repetido=not enviar_mensagem))
    chatwoot.etiquetar(chatwoot_id, [motivo.value.lower()])
    if time_id is not None:
        chatwoot.atribuir(chatwoot_id, time_id)
    chatwoot.mudar_status(chatwoot_id, "open")
    transicionar(
        sessao, conversa, EstadoConversa.ATENDIMENTO_HUMANO, motivo, laudo_id, momento=agora
    )
    if politica.handoff_expira_pix:
        expirar_pendentes(sessao, conversa.cobranca)
    if not enviar_mensagem:
        return
    nome = conversa.cobranca.cliente.nome
    if motivo is MotivoHumano.DESCONFIANCA:
        dados = DadosFixa(
            nome=nome,
            valor_centavos=None,
            link_cartao=None,
            texto=politica.texto_humano_no_whatsapp,
        )
        enviar_fixa(sessao, chatwoot, conversa, TipoFixa.HUMANO_NO_WHATSAPP, dados, agora)
        return
    dados = DadosFixa(nome=nome, valor_centavos=None, link_cartao=None)
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
