"""Comandos do agente: `agente importar` (a carteira), `agente disparar` (as aberturas),
`agente importar-ctn` (a carteira do dia pelo retrato da inadimplência do CTN real) e
`agente notas-teste` (as notas dos testadores do modo teste, em Markdown).
"""

import argparse
import functools
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, date, datetime
from pathlib import Path

from agente.abertura.disparar import disparar_aberturas
from agente.carteira.importar import importar_carteira
from agente.carteira.importar_ctn import (
    SEM_ENCERRAR_EXCESSIVO,
    SEM_ENCERRAR_INCOMPLETO,
    SEM_ENCERRAR_SUSPEITO,
    SEM_ENCERRAR_VAZIO,
    importar_do_ctn,
)
from agente.config import (
    Config,
    carregar_config,
)
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.ctn_eficaz import CTNEficaz
from agente.integracoes.disparador import DisparadorFalso
from agente.politica import LiberacaoPiloto, carregar_politica
from agente.teste.notas import relatorio_das_notas

Comando = Callable[[argparse.Namespace], int]


def _importar(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    politica = carregar_politica(config.politica_path)
    # Até a T9, não há CTN real: sem histórico, ninguém sai como propenso ao cartão.
    print("CTN falso: a propensão ao cartão fica como 'não' para todos (T9).")
    with criar_sessao(config.database_url)() as sessao:
        relatorio = importar_carteira(
            argumentos.csv, sessao, politica, CTNFalso(), campanha=argumentos.campanha
        )
    print(f"criadas: {relatorio.criadas}")
    print(f"atualizadas: {relatorio.atualizadas}")
    print(f"rejeitadas: {len(relatorio.rejeitadas)}")
    for numero, motivo in relatorio.rejeitadas:
        print(f"  linha {numero}: {motivo}")
    return 0


def _sem_detalhe_de_erro(orientacao: str) -> Callable[[Comando], Comando]:
    """Os comandos da pré-sombra lidam com conversas de clientes: uma exceção que ninguém previu
    não sai com traceback nem com a mensagem (o SQLAlchemy põe os parâmetros do SQL nela). Sai só o
    tipo do erro e o que fazer."""

    def decorar(comando: Comando) -> Comando:
        @functools.wraps(comando)
        def rodar(argumentos: argparse.Namespace) -> int:
            try:
                return comando(argumentos)
            except Exception as erro:
                print(f"Erro inesperado ({type(erro).__name__}). {orientacao}")
                return 1

        return rodar

    return decorar


def _mascarar(matricula: str) -> str:
    """Só os 3 últimos caracteres da matrícula vão à tela (LGPD)."""
    return f"***{matricula[-3:]}"


@_sem_detalhe_de_erro("Nada além das aberturas já confirmadas foi enviado; rode de novo.")
def _disparar(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    politica = carregar_politica(config.politica_path)
    if config.modo != "ativo":
        print(
            f"AGENTE_MODO é '{config.modo}': em sombra nada é disparado. Defina AGENTE_MODO=ativo "
            "(e a liberacao do politica.toml) para disparar as aberturas."
        )
        return 2
    if politica.teste is not None and argumentos.campanha == politica.teste.campanha:
        # A matrícula `SIM-` iria à API real: o disparo abre a CTNEficaz direto, sem o roteador.
        print(
            f"A campanha '{argumentos.campanha}' é a do modo teste: os testadores abrem a conversa "
            "pelo WhatsApp (1º contato), e o disparo não vale para ela."
        )
        return 2
    # Até a T4, não há disparador real: as aberturas só ficam gravadas.
    print("Disparador falso: nenhuma mensagem sai de verdade (T4).")
    # ocultar_parametros: o SQL de um erro não pode levar nome nem telefone para a tela.
    with (
        criar_sessao(config.database_url, ocultar_parametros=True)() as sessao,
        ExitStack() as pilha,
    ):
        # Com a chave do CTN, cada cobrança é conferida antes da abertura (uma vez por dia).
        ctn = pilha.enter_context(_abrir_ctn(config)) if config.ctn_real_ligado else None
        if ctn is not None:
            print("CTN real: cada cobrança é conferida antes da abertura (só leitura).")
        relatorio = disparar_aberturas(
            sessao, politica, DisparadorFalso(), argumentos.campanha, _agora(), ctn
        )
    if relatorio.fora_da_janela:
        print("fora da janela de atendimento: nada foi disparado")
        return 1
    print(f"enviadas: {relatorio.enviadas}")
    print(f"puladas: {relatorio.puladas}")
    print(f"fora da liberação (sem abertura): {relatorio.fora_da_liberacao}")
    print(f"quitadas no CTN (sem abertura): {relatorio.quitadas_no_ctn}")
    print(f"sem lançamentos no CTN (sem abertura): {relatorio.sem_lancamentos}")
    print(f"dados suspeitos do CTN (sem abertura): {relatorio.dados_suspeitos}")
    print(f"não conferidas, CTN fora (sem abertura): {relatorio.nao_conferidas}")
    print(f"telefone de mais de uma cobrança (sem abertura): {relatorio.telefone_compartilhado}")
    print(f"falhas: {len(relatorio.falhas)}")
    # Só a matrícula mascarada e o tipo do erro: a mensagem pode trazer telefone, nome ou URL.
    for matricula, erro in relatorio.falhas:
        print(f"  {_mascarar(matricula)}: {erro}")
    if relatorio.alarme_conferencia:
        print(
            "ALARME DA CONFERÊNCIA: quitadas ou sem lançamentos demais nesta rodada; a campanha "
            "parou. Confira o CTN e a carteira antes de rodar de novo."
        )
        return 3
    return 0


def _agora() -> datetime:
    return datetime.now(UTC)


def _abrir_ctn(config: Config) -> CTNEficaz:
    """O CTN real só de leitura, com as variáveis `AGENTE_CTN_*` (o teste troca esta função)."""
    return CTNEficaz.da_config(config)


@_sem_detalhe_de_erro("O que já foi gravado fica; rode de novo, a importação é idempotente.")
def _importar_ctn(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    if not config.ctn_real_ligado:
        print("Defina o AGENTE_CTN_API_KEY para ler o retrato do CTN real.")
        return 2
    politica = carregar_politica(config.politica_path)
    # O retrato sai uma vez por dia, de manhã: "hoje" é o dia do negócio, não o do servidor.
    data_ref = argumentos.data or _agora().astimezone(politica.janela.zona).date()
    if argumentos.meses_minimos < 1:
        print("--meses-minimos precisa ser ao menos 1.")
        return 2
    # No modo teste, os testadores do piloto trocam a carteira pelos casos inventados: a pessoa
    # real que usa o mesmo telefone não entra. Sem `[teste]`, o piloto é de gente real e entra.
    excluidos = (
        politica.liberacao.telefones
        if politica.teste is not None and isinstance(politica.liberacao, LiberacaoPiloto)
        else frozenset()
    )
    print(
        f"Retrato de {data_ref.isoformat()}"
        + (" (simulação: nada é gravado)" if argumentos.simular else "")
        + ". Só lê o CTN (GET); a saída traz só contagens."
    )
    # ocultar_parametros: o SQL de um erro não pode levar nome nem telefone para a tela.
    with (
        criar_sessao(config.database_url, ocultar_parametros=True)() as sessao,
        _abrir_ctn(config) as ctn,
    ):
        resumo = importar_do_ctn(
            sessao,
            ctn,
            politica,
            data_ref,
            argumentos.campanha,
            argumentos.simular,
            _agora(),
            argumentos.meses_minimos,
            excluidos,
        )
    print(f"criadas: {resumo.criadas}")
    print(f"atualizadas: {resumo.atualizadas}")
    print(f"reativadas: {resumo.reativadas}")
    print(f"encerradas: {resumo.encerradas}")
    print(f"inadimplentes distintos (1 mês ou mais): {resumo.inadimplentes}")
    print(f"ignoradas Energisa: {resumo.ignoradas_energisa}")
    print(f"abaixo do mínimo de meses: {resumo.abaixo_do_minimo}")
    print(f"ignoradas simuladas (SIM-): {resumo.ignoradas_simuladas}")
    print(f"ignoradas do piloto (telefone de testador): {resumo.ignoradas_piloto}")
    print(f"sem telefone: {resumo.sem_telefone}")
    print(f"telefones compartilhados por mais de uma matrícula: {resumo.telefones_compartilhados}")
    print(f"linhas inválidas: {resumo.linhas_invalidas}")
    print(f"linhas de outra data: {resumo.data_ref_divergente}")
    print(f"mantidas por Pix pendente: {resumo.preservadas}")
    print(f"total do retrato: {resumo.total_retrato}")
    motivo = resumo.motivo_sem_encerrar
    if motivo is None:
        return 0
    print(f"motivo de não encerrar: {motivo}")
    if motivo == SEM_ENCERRAR_INCOMPLETO:
        print("retrato incompleto: nada foi gravado nem encerrado. Rode de novo.")
        return 1
    print(_AVISO_SEM_ENCERRAR[motivo])
    return 3


_AVISO_SEM_ENCERRAR = {
    SEM_ENCERRAR_VAZIO: (
        "retrato sem inadimplentes: nada foi encerrado (o retrato do dia pode não ter saído)."
    ),
    SEM_ENCERRAR_SUSPEITO: (
        "retrato suspeito (linhas de outra data ou nenhuma da data pedida): nada foi encerrado."
    ),
    SEM_ENCERRAR_EXCESSIVO: (
        "o encerramento atingiria mais de 25% das ativas da campanha: nada foi encerrado. "
        "Confira o retrato antes de rodar de novo."
    ),
}


@_sem_detalhe_de_erro("Nada foi alterado; o comando só lê o banco.")
def _notas_teste(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    politica = carregar_politica(config.politica_path)
    with criar_sessao(config.database_url, ocultar_parametros=True)() as sessao:
        relatorio = relatorio_das_notas(sessao, politica, argumentos.desde)
    if argumentos.saida is None:
        print(relatorio)
        return 0
    argumentos.saida.parent.mkdir(parents=True, exist_ok=True)
    argumentos.saida.write_text(relatorio, encoding="utf-8")
    print(f"relatório: {argumentos.saida} (testador mascarado; notas do modo teste)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # O console do Windows não usa UTF-8 por padrão e quebra os acentos.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="agente")
    comandos = parser.add_subparsers(dest="comando", required=True)
    importar = comandos.add_parser("importar", help="importa a carteira de um CSV")
    importar.add_argument("csv", type=Path)
    importar.add_argument("--campanha", required=True)
    importar.set_defaults(rodar=_importar)
    disparar = comandos.add_parser("disparar", help="dispara as aberturas da campanha")
    disparar.add_argument("--campanha", required=True)
    disparar.set_defaults(rodar=_disparar)
    importar_ctn = comandos.add_parser(
        "importar-ctn", help="importa a carteira do dia pelo retrato da inadimplência do CTN"
    )
    importar_ctn.add_argument(
        "--data",
        type=date.fromisoformat,
        help="dia do retrato, AAAA-MM-DD (padrão: hoje, no fuso da janela)",
    )
    importar_ctn.add_argument("--campanha", default="ctn", help="nome da campanha (ctn)")
    importar_ctn.add_argument(
        "--meses-minimos",
        type=int,
        default=1,
        help="só entra quem tem este nº de meses de atraso ou mais (1; no dev2, 3: R3 a R6)",
    )
    importar_ctn.add_argument(
        "--simular", action="store_true", help="mostra os totais sem gravar nada"
    )
    importar_ctn.set_defaults(rodar=_importar_ctn)
    notas = comandos.add_parser(
        "notas-teste", help="junta as notas (/nota) dos testadores num relatório em Markdown"
    )
    notas.add_argument(
        "--desde",
        type=date.fromisoformat,
        help="só as notas deste dia em diante, AAAA-MM-DD (no fuso da janela)",
    )
    notas.add_argument("--saida", type=Path, help="grava o relatório neste arquivo (padrão: tela)")
    notas.set_defaults(rodar=_notas_teste)
    argumentos = parser.parse_args(argv)
    return argumentos.rodar(argumentos)


if __name__ == "__main__":
    sys.exit(main())
