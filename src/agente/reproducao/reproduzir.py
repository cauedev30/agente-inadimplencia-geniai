"""A reprodução da pré-sombra (etapa 10): as conversas lidas do Chatwoot, passadas em ordem de tempo
pelo mesmo caminho do serviço, em modo sombra.

Cada mensagem vira um `message_created` (`para_evento`) e entra por `receber_evento`, como no
webhook; os turnos rodam pelo `Trabalhador`, com um relógio que anda junto com os eventos. O
Chatwoot, o CTN, o Pix e o disparador são os falsos e não há transcritor nem redator: nada sai, nada
é escrito no Chatwoot, e o áudio fica "não ouvido" (a retenção da OpenAI não foi confirmada).

O relatório é agregado: contagens, sem texto de cliente nem telefone. É o que o Claude lê; a folha
de revisão (tarefa 10.4) é do Cauê.
"""

import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.api.chatwoot_webhook import receber_evento
from agente.carteira.importar import importar_carteira
from agente.config import Config
from agente.db.modelos import Cliente, Cobranca, Conversa, Turno
from agente.db.sessao import criar_sessao
from agente.dominio.telefone import normalizar_telefone
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import Jev, JevFalso
from agente.integracoes.pix import PixFalso
from agente.politica import Politica
from agente.reproducao.chatwoot_leitura import ChatwootLeitura, ConversaLida, para_evento
from agente.tipos import Modo, ResultadoTurno, StatusCobranca
from agente.turno.fila import Trabalhador
from agente.turno.orquestrador import Dependencias, processar_turno

log = logging.getLogger(__name__)

# A raiz do projeto, onde mora o alembic.ini (src/agente/reproducao/reproduzir.py -> repo/).
_RAIZ = Path(__file__).resolve().parents[3]
CAMPANHA = "pre-sombra"
# Um turno que o laço não consegue tirar da fila (um defeito) não pode prender a reprodução.
_MAX_PASSOS = 100_000
_PREFIXO_HUMANO = "HUMAN:"


@dataclass
class RelatorioReproducao:
    """Só contagens. Os dicionários são rótulo -> quantidade."""

    carteira_criadas: int = 0
    carteira_rejeitadas: int = 0
    conversas_lidas: int = 0
    # As que têm cobrança ativa na carteira pelo telefone do contato: as que o agente atenderia.
    conversas_cruzadas: int = 0
    conversas_sem_telefone: int = 0
    # As mensagens que viraram evento (a atividade e a nota privada ficam de fora, e são contadas).
    mensagens: int = 0
    mensagens_ignoradas: int = 0
    # O que o `receber_evento` respondeu a cada uma: agendado, humano, sem_cobranca, ...
    eventos: dict[str, int] = field(default_factory=dict)
    turnos_por_resultado: dict[str, int] = field(default_factory=dict)
    turnos_por_acao: dict[str, int] = field(default_factory=dict)
    # O gatilho é o motivo da saída para o humano (`HUMAN:<motivo>`).
    turnos_por_gatilho: dict[str, int] = field(default_factory=dict)
    # As vezes em que o turno foi empurrado para a abertura da janela (não cria turno gravado).
    adiados: int = 0
    # O tipo do erro (nunca a mensagem, que pode trazer dado do cliente) -> quantidade.
    erros_por_tipo: dict[str, int] = field(default_factory=dict)
    # A prova de que nada saiu: as falsas contam o que receberiam.
    chamadas_ao_chatwoot: int = 0
    templates_enviados: int = 0
    # O id da conversa no Chatwoot -> o nome do contato, só para a folha o mascarar. Fica na
    # memória: fora do `repr`, e o relatório impresso (`formatar_relatorio_reproducao`) não o lê.
    nomes_dos_contatos: dict[int, str] = field(default_factory=dict, repr=False, compare=False)


@contextmanager
def _log_sem_detalhe_de_erro() -> Iterator[None]:
    """Durante a reprodução, o log não leva o traceback nem a mensagem de uma exceção.

    Os dados são conversas reais de clientes, e a mensagem ou o trace de uma exceção de código pode
    trazer um trecho delas (a máscara do log só cobre telefone e CPF). O registro fica só com o tipo
    do erro. A fábrica de registros vale para todos os loggers e é restaurada ao sair: o log do
    serviço em produção não muda.
    """
    anterior = logging.getLogRecordFactory()

    def fabrica(*args, **kwargs) -> logging.LogRecord:
        registro = anterior(*args, **kwargs)
        erro = registro.exc_info[0] if registro.exc_info else None
        if erro is not None:
            if isinstance(registro.args, tuple):
                registro.args = tuple(
                    type(arg).__name__ if isinstance(arg, BaseException) else arg
                    for arg in registro.args
                )
            registro.msg = f"{registro.msg} [{erro.__name__}]"
        registro.exc_info = None
        registro.exc_text = None
        registro.stack_info = None
        return registro

    logging.setLogRecordFactory(fabrica)
    try:
        yield
    finally:
        logging.setLogRecordFactory(anterior)


class _Relogio:
    def __init__(self, agora: datetime):
        self.agora = agora

    def __call__(self) -> datetime:
        return self.agora


def migrar(url: str) -> None:
    """Cria o banco no esquema atual, com as mesmas migrações do serviço."""
    config = AlembicConfig(str(_RAIZ / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configurar_log"] = False
    command.upgrade(config, "head")


def _momento(mensagem: dict) -> datetime:
    return datetime.fromtimestamp(mensagem["created_at"], UTC)


def _eventos(
    leitura: ChatwootLeitura, conversas: list[ConversaLida], relatorio: RelatorioReproducao
) -> list[tuple[datetime, int, dict]]:
    """Os eventos de todas as conversas, na ordem do `created_at` (e da conversa, no empate)."""
    eventos = []
    for lida in conversas:
        for bruta in leitura.mensagens(lida.id):
            dados = para_evento(bruta, lida)
            if dados is None:
                relatorio.mensagens_ignoradas += 1
                continue
            relatorio.mensagens += 1
            eventos.append((_momento(bruta), bruta["id"], dados))
    eventos.sort(key=lambda evento: (evento[0], evento[1]))
    return eventos


def _telefones_da_carteira(sessao: Session) -> set[str]:
    consulta = (
        select(Cliente.telefone).join(Cobranca).where(Cobranca.status == StatusCobranca.ATIVA)
    )
    return set(sessao.scalars(consulta))


def _proximo_turno(fabrica: Callable[[], Session]) -> datetime | None:
    with fabrica() as sessao:
        return sessao.scalar(select(func.min(Conversa.turno_em)))


def _rodar_ate(
    fabrica: Callable[[], Session],
    trabalhador: Trabalhador,
    relogio: _Relogio,
    limite: datetime | None,
    erros: Counter,
) -> None:
    """Roda os turnos que vencem até `limite` (ou todos, sem limite), cada um no instante em que
    venceu: a janela e o humano na conversa são avaliados no tempo da conversa, não no da máquina.
    """
    ultimo = None
    repeticoes = 0
    for _ in range(_MAX_PASSOS):
        proximo = _proximo_turno(fabrica)
        if proximo is None or (limite is not None and proximo > limite):
            return
        relogio.agora = max(proximo, relogio.agora)
        trabalhador.rodar_uma_vez()
        repeticoes = repeticoes + 1 if proximo == ultimo else 0
        ultimo = proximo
        if repeticoes >= 3:
            # O turno falhou fora do tratamento do erro interno e continua vencido.
            erros["TurnoPresoNaFila"] += 1
            return
    erros["ReproducaoSemFim"] += 1


def _resumir(
    fabrica: Callable[[], Session], relatorio: RelatorioReproducao, erros: Counter
) -> None:
    resultados: Counter = Counter()
    acoes: Counter = Counter()
    gatilhos: Counter = Counter()
    with fabrica() as sessao:
        for turno in sessao.scalars(select(Turno)):
            resultados[turno.resultado.value if turno.resultado else "ABERTO"] += 1
            rotulo = turno.acao_principal
            if rotulo and rotulo.startswith(_PREFIXO_HUMANO):
                gatilhos[rotulo.removeprefix(_PREFIXO_HUMANO)] += 1
            elif rotulo:
                acoes[rotulo] += 1
            if turno.resultado is ResultadoTurno.ERRO:
                erros[(turno.redacao or {}).get("erro", "desconhecido")] += 1
    relatorio.turnos_por_resultado = dict(resultados)
    relatorio.turnos_por_acao = dict(acoes)
    relatorio.turnos_por_gatilho = dict(gatilhos)
    relatorio.erros_por_tipo = dict(erros)


@_log_sem_detalhe_de_erro()
def reproduzir(
    leitura: ChatwootLeitura,
    carteira_csv: Path,
    desde: datetime,
    banco: Path,
    politica: Politica,
    config: Config,
    jev: Jev | None = None,
) -> RelatorioReproducao:
    """Reproduz as conversas da inbox (`config.chatwoot_inbox`) com atividade desde `desde`.

    O `banco` é um SQLite novo: um arquivo que já existe é recusado, para não misturar duas
    reproduções. O Jev recebido é o que avalia (o falso, por padrão).
    """
    if config.chatwoot_inbox is None:
        raise ValueError("a reprodução precisa da inbox do Chatwoot (AGENTE_CHATWOOT_INBOX)")
    if banco.exists():
        raise FileExistsError(f"{banco} já existe: a reprodução grava num banco novo")
    url = f"sqlite:///{banco.as_posix()}"
    migrar(url)
    fabrica = criar_sessao(url)
    relatorio = RelatorioReproducao()
    erros: Counter = Counter()

    with fabrica() as sessao:
        importada = importar_carteira(carteira_csv, sessao, politica, CTNFalso(), CAMPANHA)
        telefones = _telefones_da_carteira(sessao)
    relatorio.carteira_criadas = importada.criadas + importada.atualizadas
    relatorio.carteira_rejeitadas = len(importada.rejeitadas)

    conversas = leitura.conversas(config.chatwoot_inbox, desde)
    relatorio.conversas_lidas = len(conversas)
    relatorio.nomes_dos_contatos = {lida.id: lida.nome for lida in conversas if lida.nome}
    relatorio.conversas_sem_telefone = sum(1 for lida in conversas if not lida.telefone)
    relatorio.conversas_cruzadas = sum(
        1 for lida in conversas if normalizar_telefone(lida.telefone or "") in telefones
    )
    eventos = _eventos(leitura, conversas, relatorio)

    relogio = _Relogio(eventos[0][0] if eventos else desde)
    chatwoot = ChatwootFalso()
    disparador = DisparadorFalso()
    deps = Dependencias(
        politica=politica,
        chatwoot=chatwoot,
        disparador=disparador,
        time_humano=config.chatwoot_time_humano,
        modo=Modo.SOMBRA,
        jev=jev or JevFalso(),
        ctn=CTNFalso(),
        pix=PixFalso(relogio),
    )
    adiados = 0

    def processar(sessao: Session, conversa_id: int, agora: datetime) -> None:
        nonlocal adiados
        if processar_turno(sessao, conversa_id, agora, deps) is ResultadoTurno.ADIADO:
            adiados += 1

    trabalhador = Trabalhador(fabrica, processar, relogio)
    respostas: Counter = Counter()
    for momento, _id, dados in eventos:
        # Antes de cada evento, os turnos que já venciam: a rajada fecha quando o cliente para.
        _rodar_ate(fabrica, trabalhador, relogio, momento, erros)
        relogio.agora = momento
        with fabrica() as sessao:
            respostas[receber_evento(sessao, dados, politica, config, momento)] += 1
    # No fim, avança a rajada e o que foi adiado para a abertura da janela.
    _rodar_ate(fabrica, trabalhador, relogio, None, erros)

    relatorio.eventos = dict(respostas)
    relatorio.adiados = adiados
    relatorio.chamadas_ao_chatwoot = len(chatwoot.chamadas)
    relatorio.templates_enviados = len(disparador.envios)
    _resumir(fabrica, relatorio, erros)
    log.info(
        "reproduzidas %s conversas (%s cruzadas) e %s mensagens",
        relatorio.conversas_lidas,
        relatorio.conversas_cruzadas,
        relatorio.mensagens,
    )
    return relatorio


def formatar_relatorio_reproducao(relatorio: RelatorioReproducao) -> str:
    """O relatório agregado para o terminal: contagens e rótulos, sem texto, nome nem telefone."""

    def contagens(valores: dict[str, int]) -> str:
        return ", ".join(f"{rotulo}: {n}" for rotulo, n in sorted(valores.items())) or "nenhum"

    return "\n".join(
        [
            f"carteira: {relatorio.carteira_criadas} importadas, "
            f"{relatorio.carteira_rejeitadas} rejeitadas",
            f"conversas: {relatorio.conversas_lidas} lidas, {relatorio.conversas_cruzadas} "
            f"cruzadas com a carteira, {relatorio.conversas_sem_telefone} sem telefone",
            f"mensagens: {relatorio.mensagens} ({relatorio.mensagens_ignoradas} ignoradas)",
            f"eventos: {contagens(relatorio.eventos)}",
            f"turnos por resultado: {contagens(relatorio.turnos_por_resultado)}",
            f"turnos por ação: {contagens(relatorio.turnos_por_acao)}",
            f"turnos por gatilho: {contagens(relatorio.turnos_por_gatilho)}",
            f"adiados pela janela: {relatorio.adiados}",
            f"erros por tipo: {contagens(relatorio.erros_por_tipo)}",
            f"chamadas ao Chatwoot falso: {relatorio.chamadas_ao_chatwoot}; "
            f"templates enviados: {relatorio.templates_enviados}",
        ]
    )
