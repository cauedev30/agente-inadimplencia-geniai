"""Comandos do agente: `agente importar` (a carteira), `agente disparar` (as aberturas),
`agente simular` (as conversas simuladas) e `agente comparar-redatores` (a escolha do LLM, T6).
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from agente.abertura.disparar import disparar_aberturas
from agente.carteira.importar import importar_carteira
from agente.comparacao import (
    Candidato,
    comparar_redatores,
    formatar_comparacao,
    ler_candidato,
)
from agente.config import ConfigPolitica, ConfigRedator, carregar_config
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.redator import RedatorOpenAI
from agente.politica import carregar_politica
from agente.simulacao import (
    DependenciasSimulacao,
    carregar_casos,
    formatar_relatorio,
    rodar_simulacao,
)


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


def _disparar(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    politica = carregar_politica(config.politica_path)
    # Até a T4, não há disparador real: as aberturas só ficam gravadas.
    print("Disparador falso: nenhuma mensagem sai de verdade (T4).")
    with criar_sessao(config.database_url)() as sessao:
        relatorio = disparar_aberturas(
            sessao, politica, DisparadorFalso(), argumentos.campanha, datetime.now(UTC)
        )
    if relatorio.fora_da_janela:
        print("fora da janela de atendimento: nada foi disparado")
        return 1
    print(f"enviadas: {relatorio.enviadas}")
    print(f"puladas: {relatorio.puladas}")
    print(f"falhas: {len(relatorio.falhas)}")
    for matricula, erro in relatorio.falhas:
        print(f"  {matricula}: {erro}")
    return 0


def _simular(argumentos: argparse.Namespace) -> int:
    politica = carregar_politica(argumentos.politica or ConfigPolitica().politica_path)
    # Até a T10, não há Jev real: a falsa responde o gabarito de cada turno.
    print("Jev falso: o relatório testa o encanamento, não calibra os limiares (T10).")
    casos = carregar_casos(argumentos.caminho)
    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica))
    print(formatar_relatorio(relatorio))
    return 1 if relatorio.divergencias else 0


def _comparar(argumentos: argparse.Namespace) -> int:
    config = ConfigRedator()
    if not config.llm_api_key:
        print("Defina o AGENTE_LLM_API_KEY para comparar os modelos do LLM (T6).")
        return 2
    politica = carregar_politica(argumentos.politica or config.politica_path)
    try:
        lidos = [ler_candidato(valor) for valor in argumentos.modelo]
    except ValueError as erro:
        print(erro)
        return 2
    print("Jev falso: o Jev 3 aprova todo texto; só a interpolação reprova até a T10.")
    casos = carregar_casos(argumentos.caminho)
    redatores = [
        RedatorOpenAI(
            config.llm_api_key,
            modelo,
            url=config.llm_url,
            corpo_extra=config.llm_corpo_extra,
        )
        for modelo, _, _ in lidos
    ]
    try:
        candidatos = [
            Candidato(redator, entrada, saida)
            for redator, (_, entrada, saida) in zip(redatores, lidos, strict=True)
        ]
        relatorio = comparar_redatores(candidatos, casos, DependenciasSimulacao(politica))
    finally:
        for redator in redatores:
            redator.fechar()
    print(formatar_comparacao(relatorio))
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
    simular = comandos.add_parser("simular", help="roda as conversas simuladas de uma pasta")
    simular.add_argument("caminho", type=Path)
    simular.add_argument("--politica", type=Path, help="padrão: AGENTE_POLITICA_PATH")
    simular.set_defaults(rodar=_simular)
    comparar = comandos.add_parser(
        "comparar-redatores", help="compara os LLMs candidatos nas conversas simuladas (T6)"
    )
    comparar.add_argument("caminho", type=Path)
    comparar.add_argument(
        "--modelo",
        action="append",
        required=True,
        help="modelo, ou modelo:entrada:saida com o preço em US$ por milhão de tokens "
        "(o Luna: gpt-6-luna:0.10:0.50)",
    )
    comparar.add_argument("--politica", type=Path, help="padrão: AGENTE_POLITICA_PATH")
    comparar.set_defaults(rodar=_comparar)
    argumentos = parser.parse_args(argv)
    return argumentos.rodar(argumentos)


if __name__ == "__main__":
    sys.exit(main())
