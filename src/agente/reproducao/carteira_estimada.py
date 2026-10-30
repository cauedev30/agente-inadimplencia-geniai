"""A carteira estimada das conversas (etapa 10, tarefa 10.6).

A unidade não tem a lista de inadimplentes, mas a equipe já cobra pelo WhatsApp: a mensagem da
empresa cita o débito ("seu débito é de R$ 100,20") ou o valor da chave Pix. Daí se estima quantas
mensalidades cada contato deve, e o CSV sai no formato do importador (`carteira/importar.py`) para
alimentar o `agente reproduzir`. É uma estimativa: não vem do sistema do CDT.

A leitura só lê (a pasta salva ou o Chatwoot por GET). O resultado agregado não leva texto, nome nem
telefone; as linhas do CSV levam, e por isso o arquivo fica em `pre-sombra/`, fora do git.
"""

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agente.dominio.telefone import normalizar_telefone
from agente.politica import Politica
from agente.reproducao.chatwoot_leitura import ChatwootLeitura, ConversaLida, conversa_lida

# O débito com resto de até 5 centavos ainda é um número inteiro de mensalidades (arredondamento do
# texto da operação). O valor da chave Pix tem de ser múltiplo exato.
_RESTO_MAXIMO_CENTAVOS = 5
_MATRICULA_PREFIXO = "E-"
_MATRICULA_TAMANHO = 8

# Os textos são casados depois de `_normalizar`: sem acento, em minúsculas e com um espaço só.
_LEMBRETE = re.compile(r"spc ?/ ?serasa|eu quero")
_DEBITO = re.compile(r"seu debito e de r\$ ?([\d.,]+)")
_PIX_VALOR = re.compile(r"valor: ?r\$ ?([\d.,]+)")
_PIX_CHAVE = re.compile(r"chave pix")
_PIX_REFERENTE = re.compile(r"referente")
# "R$ 1.234,56" ou "R$ 33,40": os centavos com vírgula e dois dígitos. O "6680" (sem centavos) é um
# valor quebrado da operação e não vale.
_VALOR = re.compile(r"\d+(?:\.\d{3})*,\d{2}")


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().split())


def _centavos(bruto: str) -> int | None:
    """O valor em centavos, ou None se não for dinheiro no formato "100,20".

    O ponto final colado no valor ("100,20.") é pontuação da frase.
    """
    valor = bruto.rstrip(".,")
    if not _VALOR.fullmatch(valor):
        return None
    return int(Decimal(valor.replace(".", "").replace(",", ".")) * 100)


def _mensalidades_do_debito(centavos: int, mensalidade: int) -> int | None:
    quantas = round(centavos / mensalidade)
    if quantas >= 1 and abs(centavos - quantas * mensalidade) <= _RESTO_MAXIMO_CENTAVOS:
        return quantas
    return None


def _mensalidades_do_pix(centavos: int, mensalidade: int) -> int | None:
    quantas, resto = divmod(centavos, mensalidade)
    return quantas if quantas >= 1 and resto == 0 else None


def _da_empresa(mensagens: Iterable[dict]) -> list[str]:
    """Os textos da empresa (`message_type` 1, fora a nota privada), normalizados, em ordem."""
    return [
        _normalizar(m.get("content") or "")
        for m in mensagens
        if m.get("message_type") == 1 and not m.get("private")
    ]


def _ultimo_valor(textos: list[str], padrao: re.Pattern[str]) -> str | None:
    for texto in reversed(textos):
        achado = padrao.search(texto)
        if achado:
            return achado.group(1)
    return None


@dataclass(frozen=True)
class _Cobranca:
    """O que a empresa cobrou na conversa: se cobrou e, se der, quantas mensalidades e de onde."""

    mensalidades: int | None = None
    origem: str | None = None


def _ler_cobranca(mensagens: list[dict], mensalidade: int) -> _Cobranca | None:
    """None se a empresa não cobrou nesta conversa (era só atendimento)."""
    textos = _da_empresa(mensagens)
    debito = _ultimo_valor(textos, _DEBITO)
    pix = _ultimo_valor(textos, _PIX_VALOR)
    cobrou = (
        debito is not None
        or any(_LEMBRETE.search(t) for t in textos)
        or any(_PIX_CHAVE.search(t) and _PIX_REFERENTE.search(t) for t in textos)
    )
    if not cobrou:
        return None
    if debito is not None and (centavos := _centavos(debito)) is not None:
        quantas = _mensalidades_do_debito(centavos, mensalidade)
        if quantas is not None:
            return _Cobranca(quantas, "debito")
    if pix is not None and (centavos := _centavos(pix)) is not None:
        quantas = _mensalidades_do_pix(centavos, mensalidade)
        if quantas is not None:
            return _Cobranca(quantas, "pix")
    return _Cobranca()


@dataclass(frozen=True)
class LinhaEstimada:
    """Uma linha do CSV. O nome e o telefone ficam fora do `repr`: erro e log nunca os imprimem."""

    matricula: str
    mensalidades: int
    telefone: str = field(repr=False)
    nome: str = field(repr=False)


@dataclass
class ResultadoEstimativa:
    """Só contagens, mais as linhas do CSV (que não aparecem no `repr` nem no relatório)."""

    conversas_lidas: int = 0
    de_cobranca: int = 0
    # De cobrança, mas sem telefone que se normalize: não há como cruzar com a carteira.
    sem_telefone: int = 0
    # De cobrança, com telefone, e sem um valor que dê um número de mensalidades.
    sem_valor: int = 0
    # A origem do valor (debito, pix) -> conversas. Antes de juntar os telefones repetidos.
    com_valor: dict[str, int] = field(default_factory=dict)
    telefones_unicos: int = 0
    # Mensalidades em aberto -> telefones (já sem os repetidos): o que o CSV traz.
    mensalidades: dict[int, int] = field(default_factory=dict)
    linhas: list[LinhaEstimada] = field(default_factory=list, repr=False)


def matricula_sintetica(telefone: str) -> str:
    """A matrícula inventada, estável para o mesmo telefone (a unidade não nos deu a do CTN)."""
    resumo = hashlib.sha256(telefone.encode()).hexdigest()
    return _MATRICULA_PREFIXO + resumo[:_MATRICULA_TAMANHO]


def estimar_carteira(
    conversas: Iterable[tuple[ConversaLida, list[dict]]], politica: Politica
) -> ResultadoEstimativa:
    """Estima a carteira: uma linha por telefone, da conversa de cobrança mais recente dele."""
    resultado = ResultadoEstimativa()
    com_valor: Counter[str] = Counter()
    # telefone -> (a atividade e o id da conversa, para desempatar; a linha)
    por_telefone: dict[str, tuple[tuple[datetime, int], LinhaEstimada]] = {}
    for lida, mensagens in conversas:
        resultado.conversas_lidas += 1
        cobranca = _ler_cobranca(mensagens, politica.mensalidade_centavos)
        if cobranca is None:
            continue
        resultado.de_cobranca += 1
        telefone = normalizar_telefone(lida.telefone or "")
        if telefone is None:
            resultado.sem_telefone += 1
            continue
        if cobranca.mensalidades is None or cobranca.origem is None:
            resultado.sem_valor += 1
            continue
        com_valor[cobranca.origem] += 1
        chave = (lida.ultima_atividade_em, lida.id)
        atual = por_telefone.get(telefone)
        if atual is None or chave > atual[0]:
            linha = LinhaEstimada(
                matricula=matricula_sintetica(telefone),
                mensalidades=cobranca.mensalidades,
                telefone=telefone,
                nome=lida.nome or "",
            )
            por_telefone[telefone] = (chave, linha)
    resultado.com_valor = dict(com_valor)
    resultado.linhas = [linha for _, linha in por_telefone.values()]
    resultado.telefones_unicos = len(resultado.linhas)
    resultado.mensalidades = dict(Counter(linha.mensalidades for linha in resultado.linhas))
    return resultado


def gravar_csv(resultado: ResultadoEstimativa, caminho: Path, politica: Politica) -> None:
    """Grava o CSV no formato do importador: as colunas e o separador vêm da política.

    Recusa um arquivo que já existe (`FileExistsError`); a coluna da régua fica vazia.
    """
    colunas = politica.carteira.colunas
    with caminho.open("x", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=politica.carteira.separador)
        escritor.writerow(
            [
                colunas.matricula,
                colunas.nome,
                colunas.telefone,
                colunas.mensalidades_abertas,
                colunas.regua,
            ]
        )
        for linha in resultado.linhas:
            escritor.writerow([linha.matricula, linha.nome, linha.telefone, linha.mensalidades, ""])


def conversas_da_pasta(pasta: Path) -> Iterator[tuple[ConversaLida, list[dict]]]:
    """As conversas salvas em JSON (`{"conversa", "mensagens"}`) de todas as subpastas, sem rede."""
    for arquivo in sorted(pasta.rglob("*.json")):
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            yield conversa_lida(dados["conversa"]), list(dados["mensagens"])
        except (ValueError, KeyError, TypeError) as erro:
            # Só o nome do arquivo (conv<id>.json): a mensagem do erro poderia trazer conteúdo.
            raise ValueError(
                f"{arquivo.name} não é uma conversa salva ({type(erro).__name__})"
            ) from None


def conversas_do_chatwoot(
    leitura: ChatwootLeitura, inbox_id: int, desde: datetime
) -> Iterator[tuple[ConversaLida, list[dict]]]:
    """As conversas da inbox com atividade desde `desde`, lidas do Chatwoot (só GET)."""
    for lida in leitura.conversas(inbox_id, desde):
        yield lida, leitura.mensagens(lida.id)


def formatar_resultado_estimativa(resultado: ResultadoEstimativa) -> str:
    """O resultado agregado para o terminal: contagens, sem texto, nome nem telefone."""
    origens = ", ".join(f"{o}: {n}" for o, n in sorted(resultado.com_valor.items())) or "nenhuma"
    distribuicao = (
        ", ".join(f"{m}: {n}" for m, n in sorted(resultado.mensalidades.items())) or "nenhuma"
    )
    return "\n".join(
        [
            f"conversas lidas: {resultado.conversas_lidas}",
            f"de cobrança: {resultado.de_cobranca}",
            f"  sem telefone: {resultado.sem_telefone}",
            f"  sem valor: {resultado.sem_valor}",
            f"  com valor, por origem: {origens}",
            f"telefones únicos: {resultado.telefones_unicos}",
            f"mensalidades em aberto (mensalidades: telefones): {distribuicao}",
        ]
    )
