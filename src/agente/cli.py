"""Comandos do agente: `agente importar` (a carteira), `agente disparar` (as aberturas) e
`agente simular` (as conversas simuladas).
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from agente.abertura.disparar import disparar_aberturas
from agente.carteira.importar import importar_carteira
from agente.config import carregar_config
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.disparador import DisparadorFalso
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
    politica = carregar_politica(argumentos.politica)
    # Até a T10, não há Jev real: a falsa responde o gabarito de cada turno.
    print("Jev falso: o relatório testa o encanamento, não calibra os limiares (T10).")
    casos = carregar_casos(argumentos.caminho)
    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica))
    print(formatar_relatorio(relatorio))
    return 1 if relatorio.divergencias else 0


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
    simular.add_argument("--politica", type=Path, default=Path("politica.toml"))
    simular.set_defaults(rodar=_simular)
    argumentos = parser.parse_args(argv)
    return argumentos.rodar(argumentos)


if __name__ == "__main__":
    sys.exit(main())
