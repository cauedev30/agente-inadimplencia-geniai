"""Comandos do agente: `agente importar` (a carteira), `agente disparar` (as aberturas),
`agente simular` (as conversas simuladas), `agente comparar-redatores` (a escolha do LLM, T6) e
`agente reproduzir` (a pré-sombra: as conversas reais do Chatwoot, só lidas, pelo turno em sombra).
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from agente.abertura.disparar import disparar_aberturas
from agente.carteira.importar import importar_carteira
from agente.comparacao import (
    Candidato,
    comparar_redatores,
    formatar_comparacao,
    ler_candidato,
)
from agente.config import Config, ConfigPolitica, ConfigRedator, carregar_config
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.redator import RedatorOpenAI
from agente.politica import carregar_politica
from agente.reproducao.chatwoot_leitura import ChatwootLeitura
from agente.reproducao.folha import escrever_folha
from agente.reproducao.reproduzir import formatar_relatorio_reproducao, reproduzir
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


def _agora() -> datetime:
    return datetime.now(UTC)


def _abrir_leitura(config: Config) -> ChatwootLeitura:
    """A leitura do Chatwoot com as variáveis `AGENTE_CHATWOOT_*` (o teste troca o transporte)."""
    return ChatwootLeitura(config.chatwoot_url, config.chatwoot_conta, config.chatwoot_token)


def _reproduzir(argumentos: argparse.Namespace) -> int:
    saida: Path = argumentos.saida
    banco, folha = saida / "reproducao.db", saida / "folha.md"
    # O banco da reprodução é o SQLite da pasta de saída, nunca o AGENTE_DATABASE_URL do serviço.
    config = Config(database_url=f"sqlite:///{banco.as_posix()}")
    faltam = [
        nome
        for nome, valor in (
            ("AGENTE_CHATWOOT_URL", config.chatwoot_url),
            ("AGENTE_CHATWOOT_CONTA", config.chatwoot_conta),
            ("AGENTE_CHATWOOT_INBOX", config.chatwoot_inbox),
            ("AGENTE_CHATWOOT_TOKEN", config.chatwoot_token),
        )
        if valor is None
    ]
    if faltam:
        print(f"Defina {', '.join(faltam)} para ler o Chatwoot.")
        return 2
    if argumentos.dias < 1:
        print("--dias precisa ser ao menos 1.")
        return 2
    if not argumentos.carteira.is_file():
        print(f"A carteira {argumentos.carteira} não existe.")
        return 2
    # Nada é sobrescrito: o banco mistura duas reproduções e a folha guarda os vereditos do Cauê.
    existentes = [str(caminho) for caminho in (banco, folha) if caminho.exists()]
    if existentes:
        print(f"Já existe {', '.join(existentes)}: use outra pasta em --saida ou apague o arquivo.")
        return 2
    politica = carregar_politica(config.politica_path)
    saida.mkdir(parents=True, exist_ok=True)
    print("Só lê o Chatwoot (GET); nada é enviado nem escrito lá. Jev falso até a T10.")
    desde = _agora() - timedelta(days=argumentos.dias)
    try:
        with _abrir_leitura(config) as leitura:
            relatorio = reproduzir(leitura, argumentos.carteira, desde, banco, politica, config)
    except httpx.HTTPStatusError as erro:
        # Nunca o endereço nem o cabeçalho: só o código.
        print(
            f"O Chatwoot respondeu {erro.response.status_code}: confira URL, conta, inbox e token."
        )
        print(f"Apague {banco} antes de tentar de novo.")
        return 1
    except httpx.HTTPError as erro:
        print(f"Falha de rede ao ler o Chatwoot ({type(erro).__name__}).")
        print(f"Apague {banco} antes de tentar de novo.")
        return 1
    with criar_sessao(config.database_url)() as sessao:
        turnos = escrever_folha(
            sessao, folha, amostra=argumentos.amostra, nomes=relatorio.nomes_dos_contatos
        )
    print(formatar_relatorio_reproducao(relatorio))
    print(f"folha: {folha} ({turnos} turnos; tem dado de cliente mascarado, não vai ao git)")
    print(f"banco: {banco}")
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
    reproduzir_ = comandos.add_parser(
        "reproduzir",
        help="reproduz as conversas do Chatwoot em modo sombra e escreve a folha de revisão",
    )
    reproduzir_.add_argument("--carteira", type=Path, required=True, help="CSV da carteira")
    reproduzir_.add_argument("--dias", type=int, default=3, help="quantos dias para trás (3)")
    reproduzir_.add_argument(
        "--saida", type=Path, required=True, help="pasta nova ou vazia, fora do git"
    )
    reproduzir_.add_argument("--amostra", type=int, default=50, help="turnos na folha (50)")
    reproduzir_.set_defaults(rodar=_reproduzir)
    argumentos = parser.parse_args(argv)
    return argumentos.rodar(argumentos)


if __name__ == "__main__":
    sys.exit(main())
