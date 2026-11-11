"""As notas do testador (`/nota`, D56): gravar e juntar num relatório.

O `/nota <texto>` anota uma observação presa ao último turno de verdade da conversa (o que o
testador acabou de ler). A nota fica só no banco: não vai ao LLM nem conta no teto. O relatório
(`agente notas-teste`) as reúne em Markdown para o Cauê, com o testador mascarado.
"""

from datetime import date, datetime, time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agente.db.modelos import Conversa, Mensagem, NotaTeste, Turno
from agente.integracoes.ctn_teste import numero_do_caso
from agente.logs import mascarar
from agente.politica import Politica
from agente.tipos import EstadoEnvio, OrigemMensagem

# Quantas mensagens da conversa, até a nota, entram no relatório.
MENSAGENS_NO_RELATORIO = 8
PREFIXO_DO_COMANDO = "TESTE:"


def _ultimo_turno_de_verdade(sessao: Session, conversa_id: int) -> Turno | None:
    """O último turno fechado da conversa que não foi de comando (nem a abertura do teste)."""
    return sessao.scalars(
        select(Turno)
        .where(
            Turno.conversa_id == conversa_id,
            Turno.fechado_em.is_not(None),
            or_(
                Turno.acao_principal.is_(None),
                Turno.acao_principal.not_like(f"{PREFIXO_DO_COMANDO}%"),
            ),
        )
        .order_by(Turno.id.desc())
        .limit(1)
    ).first()


def gravar_nota(
    sessao: Session, conversa: Conversa, mensagem: Mensagem, texto: str, agora: datetime
) -> NotaTeste:
    """Grava a nota (só flush; quem chama decide o commit). O `turno_id` é o do último turno
    fechado da conversa que não é de comando, ou nulo se ainda não houve nenhum."""
    turno = _ultimo_turno_de_verdade(sessao, conversa.id)
    nota = NotaTeste(
        conversa_id=conversa.id,
        cobranca_id=conversa.cobranca_id,
        turno_id=turno.id if turno is not None else None,
        mensagem_id=mensagem.id,
        caso=numero_do_caso(conversa.cobranca.cliente.matricula_ctn) or 0,
        texto=texto,
        criada_em=agora,
    )
    sessao.add(nota)
    sessao.flush()
    return nota


def _telefone_mascarado(telefone: str) -> str:
    digitos = "".join(c for c in telefone if c.isdigit())
    return "***" + digitos[-4:]


def _mensagens_ate_a_nota(sessao: Session, nota: NotaTeste) -> list[Mensagem]:
    """As últimas mensagens da conversa até a nota, em ordem, sem as do modo teste."""
    ultimas = sessao.scalars(
        select(Mensagem)
        .where(
            Mensagem.conversa_id == nota.conversa_id,
            Mensagem.id <= nota.mensagem_id,
            Mensagem.origem != OrigemMensagem.TESTE,
            Mensagem.envio != EstadoEnvio.DESCARTADA,
        )
        .order_by(Mensagem.id.desc())
        .limit(MENSAGENS_NO_RELATORIO)
    ).all()
    return list(reversed(ultimas))


def _citacao(texto: str) -> str:
    """O texto como citação do Markdown (cada quebra de linha continua a citação)."""
    return "\n> ".join(mascarar(texto).replace("\r", "").split("\n"))


def _numa_linha(texto: str) -> str:
    """O texto de uma mensagem numa linha só, para a lista."""
    return " ".join(mascarar(texto).split())


def relatorio_das_notas(sessao: Session, politica: Politica, desde: date | None) -> str:
    """As notas do teste em Markdown, da mais antiga à mais nova, a partir de `desde` (dia, no
    fuso da janela). Telefone só mascarado (`***` e os 4 últimos dígitos)."""
    zona = politica.janela.zona
    consulta = select(NotaTeste).order_by(NotaTeste.criada_em, NotaTeste.id)
    if desde is not None:
        consulta = consulta.where(NotaTeste.criada_em >= datetime.combine(desde, time.min, zona))
    notas = list(sessao.scalars(consulta))
    titulo = "# Notas do teste" + (f" (desde {desde.isoformat()})" if desde is not None else "")
    if not notas:
        return f"{titulo}\n\nNenhuma nota.\n"
    blocos = [titulo, f"{len(notas)} nota(s)."]
    for posicao, nota in enumerate(notas, start=1):
        quando = nota.criada_em.astimezone(zona).strftime("%d/%m/%Y %H:%M")
        telefone = _telefone_mascarado(nota.cobranca.cliente.telefone)
        linhas = [
            f"## Nota {posicao}: {quando}, caso {nota.caso}, testador {telefone}",
            "",
            f"> {_citacao(nota.texto)}",
            "",
        ]
        turno = nota.turno
        if turno is None:
            linhas.append("- Turno: nenhum turno do agente antes da nota.")
        else:
            resultado = turno.resultado.value if turno.resultado is not None else "sem resultado"
            linhas.append(
                f"- Turno {turno.id}: ação {turno.acao_principal or 'nenhuma'}, "
                f"resultado {resultado}."
            )
        linhas.append("- Conversa até a nota:")
        for mensagem in _mensagens_ate_a_nota(sessao, nota):
            linhas.append(f"  - {mensagem.origem.value.lower()}: {_numa_linha(mensagem.texto)}")
        blocos.append("\n".join(linhas))
    return "\n\n".join(blocos) + "\n"
