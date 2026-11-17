"""O resumo dos turnos (`agente resumo-turnos`): só contagens, para medir o agente sem ler conversa.

Mostra por onde os turnos saíram (o texto do Luna, a mensagem fixa, o atendente, o silêncio), por
que motivo o atendente assumiu, o que o código reprovou e em quantas conversas o cliente recebeu o
meio de pagamento já na 1ª resposta. Nenhum texto de cliente nem telefone entra no relatório.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Turno
from agente.politica import Politica
from agente.tipos import ResultadoTurno
from agente.turno.sinais import NAO_EXECUTADA

PREFIXO_DO_COMANDO = "TESTE:"
PREFIXO_DO_HUMANO = "HUMAN:"
# O que o turno grava como ação principal quando o teto manda ao atendente ou fecha a conversa.
ROTULOS_DO_TETO = ("HUMAN:TETO", "FIXA:CANAL_EXTERNO")


def _fora_da_conta(turno: Turno) -> str | None:
    """Por que o turno não entra na conta (sem resultado, adiado, erro ou comando de teste)."""
    if turno.acao_principal is not None and turno.acao_principal.startswith(PREFIXO_DO_COMANDO):
        return "comandos de teste"
    if turno.resultado is None:
        return "sem resultado"
    if turno.resultado is ResultadoTurno.ADIADO:
        return "adiados"
    if turno.resultado is ResultadoTurno.ERRO:
        return "erros"
    return None


def _saida(turno: Turno) -> str:
    """Por onde o turno saiu: `Luna`, `fixa`, `atendente`, `silêncio` ou `outros`."""
    if turno.resultado is ResultadoTurno.HANDOFF:
        return "atendente"
    if turno.resultado is ResultadoTurno.SEM_ENVIO:
        return "silêncio"
    gravada = turno.redacao.get("saida")
    if turno.resultado is ResultadoTurno.ENVIADO and gravada == "LLM":
        return "Luna"
    if turno.resultado is ResultadoTurno.FIXA or (
        turno.resultado is ResultadoTurno.ENVIADO and gravada in ("FRASE_FIXA", "FIXA")
    ):
        return "fixa"
    return "outros"


def _motivo_do_atendente(turno: Turno) -> str:
    """O motivo da saída ao atendente: o do rótulo `HUMAN:<motivo>`, `VALIDACAO` quando o texto
    foi reprovado, ou o erro que esgotou as tentativas."""
    rotulo = turno.acao_principal or ""
    if rotulo.startswith(PREFIXO_DO_HUMANO):
        return rotulo.removeprefix(PREFIXO_DO_HUMANO)
    if rotulo.startswith(NAO_EXECUTADA):
        return "VALIDACAO"
    return str(turno.redacao.get("erro", "sem motivo gravado"))


def _respondeu(turno: Turno) -> bool:
    """O turno mandou texto ao cliente (do Luna ou fixo); a passagem ao atendente não conta."""
    return turno.texto_enviado is not None and turno.resultado in (
        ResultadoTurno.ENVIADO,
        ResultadoTurno.FIXA,
    )


def _titulo(desde: date | None, ate: date | None) -> str:
    if desde is not None and ate is not None:
        return f"# Resumo dos turnos (de {desde.isoformat()} até {ate.isoformat()})"
    if desde is not None:
        return f"# Resumo dos turnos (desde {desde.isoformat()})"
    if ate is not None:
        return f"# Resumo dos turnos (até {ate.isoformat()})"
    return "# Resumo dos turnos"


def _media(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",")


def _secao_pagamento(por_conversa: dict[int, list[Turno]]) -> list[str]:
    """Em quantas conversas o meio de pagamento foi na 1ª resposta, quantas respostas o agente
    deu até enviá-lo e quantas bateram no teto. O meio enviado é o `forma` dos números do turno."""
    com_resposta = 0
    meio_na_primeira = 0
    ate_o_meio: list[int] = []
    no_teto = 0
    for turnos in por_conversa.values():
        if any(turno.acao_principal in ROTULOS_DO_TETO for turno in turnos):
            no_teto += 1
        respostas = [turno for turno in turnos if _respondeu(turno)]
        if not respostas:
            continue
        com_resposta += 1
        for posicao, turno in enumerate(respostas, start=1):
            if "forma" in turno.numeros:
                ate_o_meio.append(posicao)
                meio_na_primeira += posicao == 1
                break
    media = _media(sum(ate_o_meio) / len(ate_o_meio)) if ate_o_meio else "nenhuma conversa"
    return [
        "## Pagamento",
        "",
        f"- conversas com resposta do agente: {com_resposta}",
        f"- meio de pagamento na 1ª resposta: {meio_na_primeira} de {com_resposta}",
        f"- conversas que receberam o meio: {len(ate_o_meio)} de {com_resposta}",
        f"- respostas até o 1º meio, em média: {media}",
        f"- conversas que bateram no teto: {no_teto}",
    ]


def resumo_dos_turnos(
    sessao: Session, politica: Politica, desde: date | None = None, ate: date | None = None
) -> str:
    """O resumo dos turnos em Markdown, só com contagens, de `desde` a `ate` (dias inteiros, no
    fuso da janela, pelo `fechado_em`). Os turnos sem resultado, adiados, com erro e os comandos de
    teste ficam de fora da conta e aparecem numa linha à parte."""
    zona = politica.janela.zona
    consulta = select(Turno).order_by(Turno.conversa_id, Turno.id)
    if desde is not None:
        consulta = consulta.where(Turno.fechado_em >= datetime.combine(desde, time.min, zona))
    if ate is not None:
        fim = datetime.combine(ate + timedelta(days=1), time.min, zona)
        consulta = consulta.where(Turno.fechado_em < fim)
    turnos = list(sessao.scalars(consulta))
    titulo = _titulo(desde, ate)
    if not turnos:
        return f"{titulo}\n\nNenhum turno.\n"

    fora = Counter(motivo for turno in turnos if (motivo := _fora_da_conta(turno)) is not None)
    contados = [turno for turno in turnos if _fora_da_conta(turno) is None]
    saidas = Counter(_saida(turno) for turno in contados)
    outros = Counter(turno.resultado.value for turno in contados if _saida(turno) == "outros")
    atendente = [turno for turno in contados if _saida(turno) == "atendente"]
    motivos = Counter(_motivo_do_atendente(turno) for turno in atendente)
    passagens = Counter(turno.redacao.get("passagem") for turno in atendente)
    por_conversa: dict[int, list[Turno]] = {}
    for turno in contados:
        por_conversa.setdefault(turno.conversa_id, []).append(turno)

    detalhe_fora = ", ".join(
        f"{nome}: {fora[nome]}"
        for nome in ("sem resultado", "adiados", "erros", "comandos de teste")
    )
    detalhe_outros = ", ".join(f"{nome}: {total}" for nome, total in sorted(outros.items()))
    linhas = [
        titulo,
        "",
        f"Turnos contados: {len(contados)}. Fora da conta: {sum(fora.values())} ({detalhe_fora}).",
        "",
        "## Saída dos turnos",
        "",
        f"- texto do Luna: {saidas['Luna']}",
        f"- mensagem fixa: {saidas['fixa']}",
        f"- atendente: {saidas['atendente']}",
        f"- silêncio: {saidas['silêncio']}",
        f"- outros: {saidas['outros']}" + (f" ({detalhe_outros})" if detalhe_outros else ""),
        "",
        "## Atendente por motivo",
        "",
    ]
    linhas += [f"- {motivo}: {total}" for motivo, total in motivos.most_common()] or ["- nenhum"]
    linhas += [
        "",
        f"- passagem redigida pelo Luna: {passagens['LLM']}",
        f"- passagem com a frase fixa: {passagens['FIXA']}",
        "- sem passagem do Luna (gate ou texto reprovado): "
        f"{len(atendente) - passagens['LLM'] - passagens['FIXA']}",
        "",
        "## Reprovações",
        "",
        "- turnos com 2ª tentativa: "
        f"{sum(len(turno.redacao.get('tentativas', ())) >= 2 for turno in contados)}",
        f"- saídas ao atendente por VALIDACAO: {motivos['VALIDACAO']}",
        "",
    ]
    linhas += _secao_pagamento(por_conversa)
    return "\n".join(linhas) + "\n"
