"""Comandos do agente: `agente importar` (a carteira), `agente disparar` (as aberturas),
`agente simular` (as conversas simuladas), `agente comparar-redatores` (a escolha do LLM, T6) e
`agente reproduzir` (a pré-sombra: as conversas reais do Chatwoot, só lidas, pelo turno em sombra) e
`agente estimar-carteira` (a carteira estimada das conversas, para a reprodução) e
`agente importar-ctn` (a carteira do dia pelo retrato da inadimplência do CTN real).
"""

import argparse
import functools
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from agente.abertura.disparar import disparar_aberturas
from agente.carteira.importar import importar_carteira
from agente.carteira.importar_ctn import SEM_ENCERRAR_INCOMPLETO, importar_do_ctn
from agente.comparacao import (
    Candidato,
    comparar_redatores,
    formatar_comparacao,
    ler_candidato,
)
from agente.config import (
    MENSAGEM_JEV_SEM_LIBERACAO,
    Config,
    ConfigJev,
    ConfigPolitica,
    ConfigRedator,
    carregar_config,
)
from agente.db.sessao import criar_sessao
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.ctn_eficaz import CTNEficaz
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevComPrazo
from agente.integracoes.jev_typesafe import JevTypeSafe
from agente.integracoes.redator import RedatorOpenAI
from agente.politica import carregar_politica
from agente.reproducao.carteira_estimada import (
    conversas_da_pasta,
    conversas_do_chatwoot,
    estimar_carteira,
    formatar_resultado_estimativa,
    gravar_csv,
)
from agente.reproducao.chatwoot_leitura import ChatwootLeitura
from agente.reproducao.folha import AVISO_CARTEIRA_ESTIMADA, escrever_folha
from agente.reproducao.reproduzir import formatar_relatorio_reproducao, reproduzir
from agente.simulacao import (
    DependenciasSimulacao,
    carregar_casos,
    formatar_relatorio,
    rodar_simulacao,
)

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


def _disparar(argumentos: argparse.Namespace) -> int:
    config = carregar_config()
    politica = carregar_politica(config.politica_path)
    # Até a T4, não há disparador real: as aberturas só ficam gravadas.
    print("Disparador falso: nenhuma mensagem sai de verdade (T4).")
    with criar_sessao(config.database_url)() as sessao, ExitStack() as pilha:
        # Com a chave do CTN, cada cobrança é conferida antes da abertura (uma vez por dia).
        ctn = pilha.enter_context(_abrir_ctn(config)) if config.ctn_real_ligado else None
        if ctn is not None:
            print("CTN real: cada cobrança é conferida antes da abertura (só leitura).")
        relatorio = disparar_aberturas(
            sessao, politica, DisparadorFalso(), argumentos.campanha, datetime.now(UTC), ctn
        )
    if relatorio.fora_da_janela:
        print("fora da janela de atendimento: nada foi disparado")
        return 1
    print(f"enviadas: {relatorio.enviadas}")
    print(f"puladas: {relatorio.puladas}")
    print(f"quitadas no CTN (sem abertura): {relatorio.quitadas_no_ctn}")
    print(f"falhas: {len(relatorio.falhas)}")
    for matricula, erro in relatorio.falhas:
        print(f"  {matricula}: {erro}")
    return 0


def _abrir_jev(config: ConfigJev, politica) -> JevTypeSafe:
    """O Jev real com as variáveis `AGENTE_JEV_*` (o teste troca o transporte)."""
    return JevTypeSafe.da_config(config, politica.jev_timeout_ms)


def _simular(argumentos: argparse.Namespace) -> int:
    politica = carregar_politica(argumentos.politica or ConfigPolitica().politica_path)
    casos = carregar_casos(argumentos.caminho)
    if not argumentos.jev_real:
        # Sem o Jev real, a falsa responde o gabarito de cada turno.
        print("Jev falso: o relatório testa o encanamento, não calibra os limiares (T10).")
        relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica))
        print(formatar_relatorio(relatorio))
        return 1 if relatorio.divergencias else 0
    config = ConfigJev()
    if not config.jev_api_key:
        print("Defina o AGENTE_JEV_API_KEY para usar o Jev real (T10).")
        return 2
    # As conversas de tests/conversas são texto inventado: não exige a liberação dos dados reais.
    with _abrir_jev(config, politica) as jev:
        print(
            f"Jev real ({jev.origem}): só texto inventado vai à TypeSafe; gasta tokens de entrada."
        )
        deps = DependenciasSimulacao(politica, jev=JevComPrazo(jev, politica.jev_timeout_ms))
        relatorio = rodar_simulacao(casos, deps)
        print(formatar_relatorio(relatorio))
        print(f"Tokens de entrada do Jev: {jev.uso_total}")
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


def _faltam_do_chatwoot(config: Config) -> list[str]:
    return [
        nome
        for nome, valor in (
            ("AGENTE_CHATWOOT_URL", config.chatwoot_url),
            ("AGENTE_CHATWOOT_CONTA", config.chatwoot_conta),
            ("AGENTE_CHATWOOT_INBOX", config.chatwoot_inbox),
            ("AGENTE_CHATWOOT_TOKEN", config.chatwoot_token),
        )
        if valor is None
    ]


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


@_sem_detalhe_de_erro("Apague a pasta de saída antes de tentar de novo.")
def _reproduzir(argumentos: argparse.Namespace) -> int:
    saida: Path = argumentos.saida
    banco, folha = saida / "reproducao.db", saida / "folha.md"
    # O banco da reprodução é o SQLite da pasta de saída, nunca o AGENTE_DATABASE_URL do serviço.
    config = Config(database_url=f"sqlite:///{banco.as_posix()}")
    # Antes de qualquer leitura: a reprodução lê conversa real, e o Jev real ainda não pode (D12).
    if config.jev_dados_reais_bloqueados:
        print(MENSAGEM_JEV_SEM_LIBERACAO)
        return 2
    faltam = _faltam_do_chatwoot(config)
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
    real = _abrir_jev(config, politica) if config.jev_api_key else None
    jev = JevComPrazo(real, politica.jev_timeout_ms) if real else None
    qual = f"Jev real ({real.origem})" if real else "Jev falso até a T10"
    print(f"Só lê o Chatwoot (GET); nada é enviado nem escrito lá. {qual}.")
    desde = _agora() - timedelta(days=argumentos.dias)
    try:
        with _abrir_leitura(config) as leitura:
            relatorio = reproduzir(
                leitura, argumentos.carteira, desde, banco, politica, config, jev
            )
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
    finally:
        if real:
            real.fechar()
    with criar_sessao(config.database_url, ocultar_parametros=True)() as sessao:
        turnos = escrever_folha(
            sessao,
            folha,
            amostra=argumentos.amostra,
            nomes=relatorio.nomes_dos_contatos,
            aviso=AVISO_CARTEIRA_ESTIMADA if argumentos.carteira_estimada else None,
        )
    print(formatar_relatorio_reproducao(relatorio))
    print(f"folha: {folha} ({turnos} turnos; tem dado de cliente mascarado, não vai ao git)")
    print(f"banco: {banco}")
    return 0


@_sem_detalhe_de_erro("Apague o CSV de saída, se ele foi criado, antes de tentar de novo.")
def _estimar_carteira(argumentos: argparse.Namespace) -> int:
    saida: Path = argumentos.saida
    if saida.exists():
        print(f"Já existe {saida}: use outro nome em --saida ou apague o arquivo.")
        return 2
    politica = carregar_politica(ConfigPolitica().politica_path)
    if argumentos.da_pasta is not None:
        if not argumentos.da_pasta.is_dir():
            print(f"A pasta {argumentos.da_pasta} não existe.")
            return 2
        print("Lê só os JSONs da pasta; nenhuma rede.")
        try:
            resultado = estimar_carteira(conversas_da_pasta(argumentos.da_pasta), politica)
        except ValueError as erro:
            print(erro)
            return 2
    else:
        config = Config(database_url="sqlite://")
        faltam = _faltam_do_chatwoot(config)
        if faltam:
            print(f"Defina {', '.join(faltam)} para ler o Chatwoot.")
            return 2
        if argumentos.dias < 1:
            print("--dias precisa ser ao menos 1.")
            return 2
        print("Só lê o Chatwoot (GET); nada é enviado nem escrito lá.")
        desde = _agora() - timedelta(days=argumentos.dias)
        try:
            with _abrir_leitura(config) as leitura:
                resultado = estimar_carteira(
                    conversas_do_chatwoot(leitura, config.chatwoot_inbox, desde), politica
                )
        except httpx.HTTPStatusError as erro:
            # Nunca o endereço nem o cabeçalho: só o código.
            print(
                f"O Chatwoot respondeu {erro.response.status_code}: "
                "confira URL, conta, inbox e token."
            )
            return 1
        except httpx.HTTPError as erro:
            print(f"Falha de rede ao ler o Chatwoot ({type(erro).__name__}).")
            return 1
    saida.parent.mkdir(parents=True, exist_ok=True)
    gravar_csv(resultado, saida, politica)
    print(formatar_resultado_estimativa(resultado))
    print(f"carteira: {saida} (tem telefone e nome de cliente, não vai ao git)")
    print("Mensalidades estimadas das conversas, não do sistema do CDT: use --carteira-estimada.")
    return 0


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
            sessao, ctn, politica, data_ref, argumentos.campanha, argumentos.simular
        )
    print(f"criadas: {resumo.criadas}")
    print(f"atualizadas: {resumo.atualizadas}")
    print(f"encerradas: {resumo.encerradas}")
    print(f"ignoradas Energisa: {resumo.ignoradas_energisa}")
    print(f"sem telefone: {resumo.sem_telefone}")
    print(f"mantidas por Pix pendente: {resumo.preservadas}")
    print(f"total do retrato: {resumo.total_retrato}")
    if resumo.motivo_sem_encerrar == SEM_ENCERRAR_INCOMPLETO:
        print("retrato incompleto: nada foi encerrado. O que veio foi gravado; rode de novo.")
        return 1
    if resumo.motivo_sem_encerrar:
        print(
            "retrato sem inadimplentes: nada foi encerrado (o retrato do dia pode não ter saído)."
        )
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
    simular.add_argument(
        "--jev-real",
        action="store_true",
        help="usa o Jev da TypeSafe (AGENTE_JEV_API_KEY) em vez da falsa; só texto inventado",
    )
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
    reproduzir_.add_argument(
        "--carteira-estimada",
        action="store_true",
        help="a carteira veio do estimar-carteira: a folha leva o aviso no alto",
    )
    reproduzir_.set_defaults(rodar=_reproduzir)
    estimar = comandos.add_parser(
        "estimar-carteira",
        help="estima a carteira (mensalidades em aberto) a partir das conversas de cobrança",
    )
    estimar.add_argument("--saida", type=Path, required=True, help="CSV novo, fora do git")
    fonte = estimar.add_mutually_exclusive_group()
    fonte.add_argument("--da-pasta", type=Path, help="pasta de JSONs salvos, com subpastas")
    fonte.add_argument("--dias", type=int, default=30, help="ou lê o Chatwoot, N dias atrás (30)")
    estimar.set_defaults(rodar=_estimar_carteira)
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
        "--simular", action="store_true", help="mostra os totais sem gravar nada"
    )
    importar_ctn.set_defaults(rodar=_importar_ctn)
    argumentos = parser.parse_args(argv)
    return argumentos.rodar(argumentos)


if __name__ == "__main__":
    sys.exit(main())
