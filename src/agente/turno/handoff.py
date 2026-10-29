"""Saída para o humano. Toda saída do robô passa por aqui: leva ao canal externo (D5) ou, por
desconfiança de golpe, fica no próprio WhatsApp (D44)."""

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
from agente.webchat.link import ConfigWebchat, link_canal_externo

NOTA_TETO_REPETIDO = "O teto já foi atingido; o cliente escreveu de novo."


def _nota(motivo: MotivoHumano, laudos: list[Laudo], *, repetido: bool = False) -> str:
    linhas = [f"Saída para o humano: {motivo.value}"]
    if repetido:
        linhas.append(NOTA_TETO_REPETIDO)
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


def ja_desconfiou(sessao: Session, conversa: Conversa, politica: Politica) -> bool:
    """A conversa já saiu para o humano por um motivo de `motivos_humano_no_whatsapp` (D44)."""
    return _saiu_para_o_humano(sessao, conversa, *politica.motivos_humano_no_whatsapp)


def ja_saiu_pelo_teto(sessao: Session, conversa: Conversa) -> bool:
    """A conversa já recebeu a 6ª mensagem e saiu para o humano pelo teto (D33)."""
    return _saiu_para_o_humano(sessao, conversa, MotivoHumano.TETO)


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
    *,
    sem_link: bool = False,
    enviar_mensagem: bool = True,
) -> None:
    """Nota com o laudo, etiqueta, atribuição, status aberta, estado e a mensagem do canal.

    A mensagem do canal leva o link do webchat com o token da cobrança; sem `webchat` (sem o
    domínio, G7), leva o link provisório do politica.toml. Com `sem_link` (D44), ou nos motivos de
    `motivos_humano_no_whatsapp`, vai a fixa sem link e nenhum token é gerado. Sem
    `enviar_mensagem`, o cliente não recebe nada: é a volta ao humano de quem já recebeu a 6ª.
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
    if sem_link or motivo in politica.motivos_humano_no_whatsapp:
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
