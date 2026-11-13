"""Ensaio automático da demo no WhatsApp: roteiros contra o serviço montado como na demo.

Uso, de dentro de repo/ (com a demo parada: a máquina tem pouca memória):
    python -m uv run python ferramentas/demo-whatsapp/ensaiar.py --kit ../pre-sombra/demo-whatsapp

O modo teste (D56, o cardápio de 5 casos e os comandos `/caso`, `/paguei`, `/nota`) tem política e
roteiros próprios, só com dado inventado:
    python -m uv run python ferramentas/demo-whatsapp/ensaiar.py --kit <kit>
        --teste ferramentas/demo-whatsapp/politica-teste-ensaio.toml
        --roteiros ferramentas/demo-whatsapp/roteiros/teste.yaml
Com `--teste` não há carteira: o caso nasce no 1º contato.

O serviço sobe igual ao da demo (`agente.api.principal:montar_app`, o ambiente de
`subir.montar_ambiente`, o Luna real), mas com um Chatwoot falso local no lugar do real: a
URL do Chatwoot é sempre a do falso, e o ensaio nem lê o `chatwoot-teste.local.json`. Só a chave
do Luna é lida (a variável `AGENTE_LLM_API_KEY` ou o `openai.local.json`). Só texto inventado vai
ao Luna.

Cada roteiro (`roteiros/teste.yaml`) tem um devedor inventado, com telefone próprio. Cada mensagem
do cliente chega em dobro ao webhook, como no Chatwoot real (o bot e o webhook mandam o mesmo
evento). A saída fica numa pasta nova do kit, `ensaio-AAAAMMDD-HHMM/`, fora do git: a política, a
carteira, o banco, o `servico.log` e o `relatorio.md`.
"""

import argparse
import dataclasses
import importlib
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import count
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agente.politica import Regua
from agente.teste.comandos import ROTULO_ENCERRADO, NomeDoComando
from agente.tipos import (
    EstadoConversa,
    MotivoHumano,
    ResultadoTurno,
    StatusCobranca,
    TipoFixa,
)
from agente.turno.acoes import Acao
from agente.turno.sinais import NAO_EXECUTADA, rotulo_humano

# Os módulos irmãos (a pasta `demo-whatsapp` tem hífen, não é pacote): a pasta entra no sys.path
# aqui, para valer rodando o script e carregando-o no teste. `import_module` evita um import fora
# do topo.
_PASTA = Path(__file__).resolve().parent
if str(_PASTA) not in sys.path:
    sys.path.insert(0, str(_PASTA))
subir = importlib.import_module("subir")
chatwoot_falso = importlib.import_module("chatwoot_falso")

ErroDaDemo = subir.ErroDaDemo
ChatwootFalsoLocal = chatwoot_falso.ChatwootFalsoLocal

REPO = subir.REPO
ROTEIROS_PADRAO = _PASTA / "roteiros" / "teste.yaml"
# O Chatwoot falso se passa pela conta e pela inbox da demo, que o serviço filtra por padrão.
CONTA = 91
INBOX = 280
PRIMEIRA_CONVERSA = 9000
PRAZO_DO_TURNO_SEGUNDOS = 120
MARCAS_DE_ERRO_NO_LOG = ("ERROR", "Traceback")
_NOMES = (
    "Ana",
    "Bruno",
    "Carla",
    "Daniel",
    "Elisa",
    "Fabio",
    "Gabriela",
    "Henrique",
    "Isabela",
    "Jorge",
    "Karina",
    "Lucas",
)


class ErroDoEnsaio(ErroDaDemo):
    """Falha durante o ensaio (o webhook recusou a mensagem, o serviço caiu)."""


# --------------------------------------------------------------------------- roteiros


class _Imutavel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# O que o modo teste grava em `acao_principal` (`teste/comandos.py`): `TESTE:<COMANDO>`, a abertura
# do 1º contato e o caso encerrado.
_ROTULOS_DO_TESTE = {f"TESTE:{nome.upper()}" for nome in get_args(NomeDoComando)} | {
    "TESTE:ABERTURA",
    ROTULO_ENCERRADO,
}


def _acao_conhecida(valor: str) -> bool:
    """Os rótulos que o turno grava em `acao_principal`."""
    if valor in {rotulo_humano(motivo) for motivo in MotivoHumano}:
        return True
    prefixo, _, resto = valor.partition(":")
    if prefixo + ":" == NAO_EXECUTADA:
        return _acao_conhecida(resto)
    if prefixo == "FIXA":
        return resto in {tipo.value for tipo in TipoFixa}
    if valor == "REINICIO" or valor in _ROTULOS_DO_TESTE:
        return True
    return valor in {acao.value for acao in Acao if acao is not Acao.HUMAN}


class Espera(_Imutavel):
    """O que o passo deve produzir. A `acao` compara com `turno.acao_principal`."""

    acao: str | None = None
    resultado: ResultadoTurno | None = None
    texto_tem: list[str] = []
    texto_nao_tem: list[str] = []
    # Trechos que alguma mensagem ao Chatwoot da janela do passo deve ter (a confirmação do /reset
    # sai fora do turno que o ensaio espera, então o `texto_tem` não a vê).
    mensagem_tem: list[str] = []
    etiqueta: str | None = None

    @field_validator("acao")
    @classmethod
    def _acao_valida(cls, valor: str | None):
        if valor is not None and not _acao_conhecida(valor):
            raise ValueError(f"ação desconhecida: {valor}")
        return valor


class Passo(_Imutavel):
    # O texto do cliente. Pode ficar vazio só quando o passo manda um `anexo` (a foto sem texto).
    cliente: str = ""
    # O tipo do anexo que acompanha a mensagem (`image` ou `file`): o Luna não os lê (AUD-07).
    anexo: Literal["image", "file"] | None = None
    # Manda esta mensagem N s depois da anterior, sem esperar o turno dela (o caso do /reset).
    intervalo: float | None = Field(default=None, ge=0)
    espera: Espera | None = None
    # O "certo" é decisão do Cauê: o relatório mostra o que veio, sem veredito.
    a_decidir: bool = False

    @model_validator(mode="after")
    def _texto_ou_anexo(self):
        if not self.cliente.strip() and self.anexo is None:
            raise ValueError("informe o texto do cliente ou um anexo")
        return self


class Fim(_Imutavel):
    """O estado da conversa do devedor e, no modo teste, da cobrança dela no fim do roteiro."""

    estado: EstadoConversa | None = None
    motivo: MotivoHumano | None = None
    # O status (ATIVA, PAGA, ENCERRADA) e as mensalidades em aberto da cobrança da conversa viva.
    cobranca: StatusCobranca | None = None
    mensalidades: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _algo_a_conferir(self):
        if (
            self.estado is None
            and self.motivo is None
            and self.cobranca is None
            and self.mensalidades is None
        ):
            raise ValueError("informe o estado, o motivo, a cobranca ou as mensalidades")
        return self


class Roteiro(_Imutavel):
    nome: str = Field(min_length=1)
    regua: Regua = Regua.R3
    mensalidades: int = Field(default=3, ge=1)
    # O /reset só vale para a equipe. Hoje todos os telefones do ensaio entram em `telefones_equipe`
    # (a política tem uma lista só); o campo documenta quem usa o comando.
    equipe: bool = False
    passos: list[Passo] = Field(min_length=1)
    fim: Fim | None = None


class RoteiroInvalido(ErroDaDemo):
    """Um roteiro com um campo errado; a mensagem diz o arquivo e o campo."""


def carregar_roteiros(caminho: Path) -> list[Roteiro]:
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RoteiroInvalido(f"falta o arquivo de roteiros {caminho}") from None
    except yaml.YAMLError as erro:
        raise RoteiroInvalido(f"{caminho}: o YAML é inválido ({erro})") from None
    if not isinstance(dados, list) or not dados:
        raise RoteiroInvalido(f"{caminho}: o arquivo tem de ser uma lista de roteiros")
    roteiros = []
    for posicao, bruto in enumerate(dados, start=1):
        try:
            roteiros.append(Roteiro.model_validate(bruto))
        except ValidationError as erro:
            campos = "; ".join(
                f"{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}" for e in erro.errors()
            )
            raise RoteiroInvalido(f"{caminho}: roteiro {posicao}: {campos}") from erro
    nomes = [roteiro.nome for roteiro in roteiros]
    repetidos = sorted({nome for nome in nomes if nomes.count(nome) > 1})
    if repetidos:
        raise RoteiroInvalido(f"{caminho}: nome repetido: {', '.join(repetidos)}")
    return roteiros


# --------------------------------------------------------------------------- kit


def telefone_do_roteiro(numero: int) -> str:
    """O telefone inventado do roteiro `numero` (1, 2, …): +5583900000NNN."""
    return f"+5583900000{numero:03d}"


def politica_do_ensaio(texto_da_politica: str, telefones: list[str]) -> str:
    """Troca a liberação e a equipe da política pelos telefones inventados do ensaio."""
    lista = json.dumps(telefones)
    trocas = {
        "telefones_equipe": f"telefones_equipe = {lista}  # ENSAIO: só os telefones inventados",
        "liberacao": (
            f'liberacao = {{ modo = "piloto", telefones = {lista} }}  '
            "# ENSAIO: só os telefones inventados"
        ),
    }
    for chave, linha in trocas.items():
        padrao = re.compile(rf"^{chave}\s*=.*$", re.MULTILINE)
        if len(padrao.findall(texto_da_politica)) != 1:
            raise ErroDaDemo(
                f"a política do kit precisa ter exatamente uma linha `{chave} = ...` (de início de "
                "linha) para o ensaio trocar"
            )
        texto_da_politica = padrao.sub(lambda _achado, linha=linha: linha, texto_da_politica)
    return texto_da_politica


def carteira_do_ensaio(roteiros: list[Roteiro]) -> str:
    """O CSV no formato da carteira da demo, com um devedor inventado por roteiro."""
    linhas = ["matricula;nome;telefone;mensalidades_abertas;regua"]
    for numero, roteiro in enumerate(roteiros, start=1):
        nome = f"{_NOMES[(numero - 1) % len(_NOMES)]} Ensaio"
        linhas.append(
            f"ENS-{numero:04d};{nome};{telefone_do_roteiro(numero)};"
            f"{roteiro.mensalidades};{roteiro.regua.value}"
        )
    return "\n".join(linhas) + "\n"


_CARDAPIO = re.compile(r'^cardapio\s*=\s*"([^"]+)"[^\n]*$', re.MULTILINE)


def _cardapio_do_ensaio(texto_da_politica: str, politica: Path) -> tuple[str, Path]:
    """A política de teste com o `cardapio` trocado para o arquivo que o ensaio copia para `saida`
    (o caminho original é relativo à pasta da política, que não é a da saída)."""
    achados = _CARDAPIO.findall(texto_da_politica)
    if len(achados) != 1:
        raise ErroDaDemo(f"{politica}: precisa de exatamente uma linha `cardapio = '...'`")
    origem = politica.parent / achados[0]
    if not origem.is_file():
        raise ErroDaDemo(f"{politica}: o cardápio {origem} não existe")
    nova = _CARDAPIO.sub(
        lambda _achado: f'cardapio = "{origem.name}"  # ENSAIO: copiado para a pasta do ensaio',
        texto_da_politica,
    )
    return nova, origem


def montar_kit(
    kit_demo: Path, saida: Path, roteiros: list[Roteiro], teste: Path | None = None
) -> dict[str, str]:
    """Grava a política do ensaio (e a carteira, ou o cardápio) em `saida`; devolve o telefone de
    cada roteiro.

    Sem `teste`, a política é a `politica-demo.toml` do kit e há um devedor por roteiro na carteira.
    Com `teste`, a política é a de teste (com o `[teste]`), o cardápio vai junto e não há carteira:
    o caso nasce no 1º contato.
    """
    telefones = {roteiro.nome: telefone_do_roteiro(n) for n, roteiro in enumerate(roteiros, 1)}
    cardapio = None
    if teste is None:
        origem = kit_demo / "politica-demo.toml"
        if not origem.is_file():
            raise ErroDaDemo(f"falta politica-demo.toml no kit ({kit_demo})")
        texto = origem.read_text(encoding="utf-8")
    else:
        if not teste.is_file():
            raise ErroDaDemo(f"falta a política de teste {teste}")
        texto, cardapio = _cardapio_do_ensaio(teste.read_text(encoding="utf-8"), teste)
    politica = politica_do_ensaio(texto, list(telefones.values()))
    saida.mkdir(parents=True, exist_ok=True)
    (saida / "politica-demo.toml").write_text(politica, encoding="utf-8")
    if cardapio is None:
        (saida / "carteira-ensaio.csv").write_text(carteira_do_ensaio(roteiros), encoding="utf-8")
    else:
        (saida / cardapio.name).write_text(cardapio.read_text(encoding="utf-8"), encoding="utf-8")
    return telefones


# --------------------------------------------------------------------------- motor


class Veredito(StrEnum):
    OK = "OK"
    DIVERGIU = "DIVERGIU"
    ERRO = "ERRO"
    SEM_TURNO = "SEM TURNO"
    A_DECIDIR = "A DECIDIR"


@dataclass(frozen=True)
class Obtido:
    """O que o turno fez, lido do banco, mais o que o Chatwoot falso e o log viram no passo."""

    acao_principal: str | None
    acoes_anexadas: list[str]
    resultado: str | None
    texto_enviado: str | None
    # Uma lista por tentativa de redação: o que reprovou o texto.
    reprovadas: list[list[str]]
    estado: str | None
    motivo: str | None
    segundos: float = 0.0
    registros_chatwoot: list = field(default_factory=list)
    erros_no_log: list[str] = field(default_factory=list)
    # O que não estraga o turno mas tem de aparecer: o POST em dobro com 500 e o erro dele no log.
    avisos: list[str] = field(default_factory=list)


def evento_do_cliente(
    mensagem_id: int,
    conversa_id: int,
    telefone: str,
    texto: str,
    conta: int,
    inbox: int,
    anexo: str | None = None,
) -> dict:
    """O `message_created` de uma mensagem do cliente, no formato que `ler_evento` lê. Com `anexo`
    (`image` ou `file`), a mensagem leva esse anexo, sem endereço (o agente não o baixa)."""
    evento = {
        "event": "message_created",
        "id": mensagem_id,
        "message_type": "incoming",
        "private": False,
        "content": texto,
        "account": {"id": conta},
        "inbox": {"id": inbox},
        "sender": {"type": "contact", "phone_number": telefone},
        "conversation": {
            "id": conversa_id,
            "status": "pending",
            "meta": {"sender": {"phone_number": telefone}},
        },
    }
    if anexo is not None:
        evento["attachments"] = [{"file_type": anexo}]
    return evento


def postar(url_webhook: str, evento: dict, em_dobro: bool = True) -> list[int]:
    """Manda o evento ao webhook e devolve os códigos. Em dobro, os dois POSTs saem juntos.

    O Chatwoot real manda cada evento pelo bot e pelo webhook; mandar juntos pega a corrida na
    deduplicação. Basta um 200 para a mensagem ter sido gravada: o outro código vira aviso no passo
    (A8, a corrida na 1ª mensagem de uma conversa nova). Sem nenhum 200, falha. O erro nunca cita
    a URL, que leva o segredo do webhook.
    """

    def _um() -> int:
        try:
            return httpx.post(url_webhook, json=evento, timeout=60).status_code
        except httpx.HTTPError as erro:
            raise ErroDoEnsaio(
                f"não consegui falar com o serviço ({type(erro).__name__})"
            ) from None

    envios = 2 if em_dobro else 1
    with ThreadPoolExecutor(max_workers=envios) as pool:
        codigos = [futuro.result() for futuro in [pool.submit(_um) for _ in range(envios)]]
    if 200 not in codigos:
        raise ErroDoEnsaio(f"o webhook respondeu {codigos} (nenhum 200)")
    return codigos


def _abrir_leitura(banco: Path) -> sqlite3.Connection:
    # Só leitura: o serviço é o único que escreve no banco do ensaio.
    conexao = sqlite3.connect(f"{banco.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conexao.row_factory = sqlite3.Row
    return conexao


def _json(valor: Any, padrao: Any) -> Any:
    if valor is None:
        return padrao
    return json.loads(valor) if isinstance(valor, str | bytes) else valor


def _ler_turno_fechado(banco: Path, chatwoot_message_id: str) -> Obtido | None:
    with closing(_abrir_leitura(banco)) as con:
        mensagem = con.execute(
            "SELECT id FROM mensagem WHERE chatwoot_message_id = ?", (chatwoot_message_id,)
        ).fetchone()
        if mensagem is None:
            return None
        turnos = con.execute(
            "SELECT * FROM turno WHERE resultado IS NOT NULL "
            "AND resultado NOT IN ('ADIADO', 'ERRO') ORDER BY id"
        ).fetchall()
        turno = next((t for t in turnos if mensagem["id"] in _json(t["mensagens_ids"], [])), None)
        if turno is None:
            return None
        conversa = con.execute(
            "SELECT estado, motivo_humano FROM conversa WHERE id = ?", (turno["conversa_id"],)
        ).fetchone()
    redacao = _json(turno["redacao"], {})
    tentativas = redacao.get("tentativas", []) if isinstance(redacao, dict) else []
    return Obtido(
        acao_principal=turno["acao_principal"],
        acoes_anexadas=list(_json(turno["acoes_anexadas"], [])),
        resultado=turno["resultado"],
        texto_enviado=turno["texto_enviado"],
        reprovadas=[list(t.get("reprovadas") or []) for t in tentativas],
        estado=conversa["estado"] if conversa else None,
        motivo=conversa["motivo_humano"] if conversa else None,
    )


def esperar_turno(
    banco: Path,
    chatwoot_message_id: str,
    prazo_s: float = PRAZO_DO_TURNO_SEGUNDOS,
    *,
    intervalo_s: float = 0.5,
    inicio: float | None = None,
) -> Obtido | None:
    """Espera o turno fechado (nem nulo, nem ADIADO, nem ERRO) que contém a mensagem; None no prazo.

    O turno ERRO (falha passageira do Luna, AUD-03) é tentado de novo; só a nova tentativa vale.

    O tempo (`segundos`) conta do `inicio` (um `time.perf_counter()`; o padrão é agora), porque os
    campos `aberto_em` e `fechado_em` do turno não servem de relógio.
    """
    inicio = time.perf_counter() if inicio is None else inicio
    limite = inicio + prazo_s
    while True:
        try:
            obtido = _ler_turno_fechado(banco, chatwoot_message_id)
        except sqlite3.OperationalError:
            # O banco travado pelo serviço, ou ainda sem as tabelas: tenta de novo.
            obtido = None
        if obtido is not None:
            return dataclasses.replace(obtido, segundos=time.perf_counter() - inicio)
        if time.perf_counter() >= limite:
            return None
        time.sleep(intervalo_s)


def ler_conversa(banco: Path, chatwoot_conversa_id: int) -> tuple[str, str | None] | None:
    """O estado e o motivo da conversa ligada à conversa do Chatwoot; None se não houver."""
    try:
        with closing(_abrir_leitura(banco)) as con:
            linha = con.execute(
                "SELECT estado, motivo_humano FROM conversa WHERE chatwoot_conversa_id = ?",
                (chatwoot_conversa_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (linha["estado"], linha["motivo_humano"]) if linha else None


def ler_cobranca(banco: Path, chatwoot_conversa_id: int) -> tuple[str, int] | None:
    """O status e as mensalidades em aberto da cobrança da conversa ligada à conversa do Chatwoot;
    None se não houver."""
    try:
        with closing(_abrir_leitura(banco)) as con:
            linha = con.execute(
                "SELECT cobranca.status AS status, cobranca.mensalidades_abertas AS abertas "
                "FROM conversa JOIN cobranca ON cobranca.id = conversa.cobranca_id "
                "WHERE conversa.chatwoot_conversa_id = ?",
                (chatwoot_conversa_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (linha["status"], linha["abertas"]) if linha else None


def divergencias(espera: Espera | None, obtido: Obtido) -> list[str]:
    """O que o passo esperava e não veio, em frases curtas."""
    if espera is None:
        return []
    achadas = []
    if espera.acao is not None and obtido.acao_principal != espera.acao:
        achadas.append(f"ação: esperava {espera.acao}, veio {obtido.acao_principal}")
    if espera.resultado is not None and obtido.resultado != espera.resultado:
        achadas.append(f"resultado: esperava {espera.resultado.value}, veio {obtido.resultado}")
    texto = (obtido.texto_enviado or "").casefold()
    for trecho in espera.texto_tem:
        if trecho.casefold() not in texto:
            achadas.append(f"o texto não tem «{trecho}»")
    for trecho in espera.texto_nao_tem:
        if trecho.casefold() in texto:
            achadas.append(f"o texto tem «{trecho}», que não devia ter")
    enviadas = [
        str(r.conteudo).casefold() for r in obtido.registros_chatwoot if r.tipo == "mensagem"
    ]
    for trecho in espera.mensagem_tem:
        if not any(trecho.casefold() in enviada for enviada in enviadas):
            achadas.append(f"nenhuma mensagem enviada tem «{trecho}»")
    if espera.etiqueta is not None:
        etiquetas = {
            e.casefold()
            for r in obtido.registros_chatwoot
            if r.tipo == "etiquetas"
            for e in r.conteudo
        }
        if espera.etiqueta.casefold() not in etiquetas:
            achadas.append(f"a etiqueta «{espera.etiqueta}» não foi posta")
    return achadas


def conferir(espera: Espera | None, a_decidir: bool, obtido: Obtido | None) -> Veredito:
    """O veredito do passo. Sem turno é SEM TURNO; o erro vale mais que o "a decidir"."""
    if obtido is None:
        return Veredito.SEM_TURNO
    if obtido.resultado == ResultadoTurno.ERRO or obtido.erros_no_log:
        return Veredito.ERRO
    if a_decidir:
        return Veredito.A_DECIDIR
    return Veredito.DIVERGIU if divergencias(espera, obtido) else Veredito.OK


def divergencias_do_fim(
    fim: Fim,
    estado: str | None,
    motivo: str | None,
    cobranca: str | None = None,
    mensalidades: int | None = None,
) -> list[str]:
    achadas = []
    if fim.estado is not None and estado != fim.estado:
        achadas.append(f"estado: esperava {fim.estado.value}, veio {estado}")
    if fim.motivo is not None and motivo != fim.motivo:
        achadas.append(f"motivo: esperava {fim.motivo.value}, veio {motivo}")
    if fim.cobranca is not None and cobranca != fim.cobranca:
        achadas.append(f"cobrança: esperava {fim.cobranca.value}, veio {cobranca}")
    if fim.mensalidades is not None and mensalidades != fim.mensalidades:
        achadas.append(f"mensalidades: esperava {fim.mensalidades}, veio {mensalidades}")
    return achadas


@dataclass(frozen=True)
class ResultadoPasso:
    indice: int
    passo: Passo
    veredito: Veredito
    obtido: Obtido | None
    divergencias: list[str] = field(default_factory=list)
    # False: a mensagem seguinte foi mandada sem esperar o turno deste (o `intervalo` do próximo).
    esperou_turno: bool = True


@dataclass(frozen=True)
class ResultadoFim:
    veredito: Veredito
    estado: str | None
    motivo: str | None
    divergencias: list[str] = field(default_factory=list)
    cobranca: str | None = None
    mensalidades: int | None = None


@dataclass(frozen=True)
class ResultadoRoteiro:
    roteiro: Roteiro
    telefone: str
    passos: list[ResultadoPasso]
    fim: ResultadoFim | None = None
    # O roteiro parou antes do fim (o webhook recusou a mensagem, por exemplo).
    interrompido: str | None = None

    @property
    def vereditos(self) -> list[Veredito]:
        itens = [p.veredito for p in self.passos]
        return itens + ([self.fim.veredito] if self.fim else [])

    @property
    def ok(self) -> int:
        return sum(1 for v in self.vereditos if v is Veredito.OK)

    @property
    def total(self) -> int:
        # Um roteiro interrompido conta os passos que não rodaram.
        previstos = len(self.roteiro.passos) + (1 if self.roteiro.fim else 0)
        return max(previstos, len(self.vereditos))


@dataclass
class Rodada:
    """O que o motor precisa para rodar os roteiros contra o serviço."""

    url_webhook: str
    banco: Path
    log: Path
    falso: Any  # ChatwootFalsoLocal
    conta: int = CONTA
    inbox: int = INBOX
    prazo_s: float = PRAZO_DO_TURNO_SEGUNDOS
    ids: Iterator[int] = field(default_factory=lambda: count(100_000))
    pausa: Callable[[float], None] = time.sleep
    servico_vivo: Callable[[], bool] = lambda: True
    intervalo_s: float = 0.5
    # Quanto esperar pelas mensagens que o passo exige (`mensagem_tem`) e que saem depois de o turno
    # fechar (a confirmação do `/paguei` sai de uma rotina, fora do turno).
    espera_mensagens_s: float = 20.0


def _tamanho(arquivo: Path) -> int:
    try:
        return arquivo.stat().st_size
    except FileNotFoundError:
        return 0


def erros_do_log(log: Path, desde: int) -> list[str]:
    """As linhas com ERROR ou Traceback que o serviço escreveu depois do byte `desde`."""
    try:
        with log.open("rb") as arquivo:
            arquivo.seek(desde)
            novo = arquivo.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return [
        linha.strip()[:300]
        for linha in novo.splitlines()
        if any(marca in linha for marca in MARCAS_DE_ERRO_NO_LOG)
    ][:5]


def esperar_mensagens(
    falso: Any,
    desde: float,
    conversa_id: int,
    trechos: list[str],
    prazo_s: float,
    *,
    intervalo_s: float = 0.2,
) -> list:
    """Os registros do Chatwoot falso da conversa depois de `desde`, esperando até `prazo_s` que
    cada trecho de `trechos` apareça em alguma mensagem enviada. No prazo, devolve o que houver (a
    divergência sai depois, na conferência)."""
    limite = time.perf_counter() + prazo_s
    while True:
        registros = [r for r in falso.registros_desde(desde) if r.conversa_id in (0, conversa_id)]
        enviadas = [str(r.conteudo).casefold() for r in registros if r.tipo == "mensagem"]
        completo = all(any(t.casefold() in e for e in enviadas) for t in trechos)
        if completo or time.perf_counter() >= limite:
            return registros
        time.sleep(intervalo_s)


def rodar_roteiro(
    roteiro: Roteiro, numero: int, telefone: str, rodada: Rodada, conversa_id: int | None = None
) -> ResultadoRoteiro:
    """Manda cada mensagem do roteiro, espera o turno e confere. Para no primeiro erro de envio."""
    conversa_id = PRIMEIRA_CONVERSA + numero if conversa_id is None else conversa_id
    resultados: list[ResultadoPasso] = []
    # Quando o passo anterior não esperou o turno, a janela dos registros e do log continua nele.
    marca = None
    posicao_no_log = 0
    for indice, passo in enumerate(roteiro.passos, start=1):
        if not rodada.servico_vivo():
            raise ErroDoEnsaio("o serviço caiu: veja o servico.log da pasta do ensaio")
        if passo.intervalo is not None and indice > 1:
            rodada.pausa(passo.intervalo)
        if marca is None:
            marca = time.perf_counter()
            posicao_no_log = _tamanho(rodada.log)
        mensagem_id = next(rodada.ids)
        evento = evento_do_cliente(
            mensagem_id,
            conversa_id,
            telefone,
            passo.cliente,
            rodada.conta,
            rodada.inbox,
            passo.anexo,
        )
        inicio = time.perf_counter()
        try:
            codigos = postar(rodada.url_webhook, evento)
        except ErroDoEnsaio as erro:
            resultados.append(ResultadoPasso(indice, passo, Veredito.ERRO, None, [str(erro)]))
            return ResultadoRoteiro(roteiro, telefone, resultados, None, str(erro))
        proximo = roteiro.passos[indice] if indice < len(roteiro.passos) else None
        if proximo is not None and proximo.intervalo is not None:
            resultados.append(ResultadoPasso(indice, passo, Veredito.OK, None, esperou_turno=False))
            continue
        obtido = esperar_turno(
            rodada.banco,
            str(mensagem_id),
            rodada.prazo_s,
            intervalo_s=rodada.intervalo_s,
            inicio=inicio,
        )
        if obtido is not None:
            erros = erros_do_log(rodada.log, posicao_no_log)
            avisos = []
            if any(codigo != 200 for codigo in codigos):
                # O erro no log é o do POST que perdeu a corrida (A8): vira aviso. Um erro do
                # próprio turno continua pego pelo resultado ERRO no banco.
                avisos = [f"o webhook respondeu {codigos} (A8)", *erros]
                erros = []
            exigidas = passo.espera.mensagem_tem if passo.espera else []
            obtido = dataclasses.replace(
                obtido,
                registros_chatwoot=esperar_mensagens(
                    rodada.falso,
                    marca,
                    conversa_id,
                    exigidas,
                    rodada.espera_mensagens_s if exigidas else 0,
                    intervalo_s=rodada.intervalo_s,
                ),
                erros_no_log=erros,
                avisos=avisos,
            )
        veredito = conferir(passo.espera, passo.a_decidir, obtido)
        achadas = divergencias(passo.espera, obtido) if obtido is not None else []
        if obtido is None:
            achadas = [f"nenhum turno fechou em {rodada.prazo_s:.0f} s"]
        resultados.append(ResultadoPasso(indice, passo, veredito, obtido, achadas))
        marca = None
    fim = None
    if roteiro.fim is not None:
        conversa = ler_conversa(rodada.banco, conversa_id)
        estado, motivo = conversa if conversa else (None, None)
        cobranca = ler_cobranca(rodada.banco, conversa_id)
        status, abertas = cobranca if cobranca else (None, None)
        achadas = divergencias_do_fim(roteiro.fim, estado, motivo, status, abertas)
        fim = ResultadoFim(
            Veredito.DIVERGIU if achadas else Veredito.OK,
            estado,
            motivo,
            achadas,
            status,
            abertas,
        )
    return ResultadoRoteiro(roteiro, telefone, resultados, fim)


# --------------------------------------------------------------------------- relatório


def _curto(texto: str, limite: int = 200) -> str:
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def _esperado(passo: Passo) -> str:
    espera = passo.espera
    if espera is None:
        return "sem expectativa"
    partes = []
    if espera.acao:
        partes.append(f"ação {espera.acao}")
    if espera.resultado:
        partes.append(f"resultado {espera.resultado.value}")
    if espera.texto_tem:
        partes.append("texto com " + ", ".join(f"«{t}»" for t in espera.texto_tem))
    if espera.texto_nao_tem:
        partes.append("texto sem " + ", ".join(f"«{t}»" for t in espera.texto_nao_tem))
    if espera.mensagem_tem:
        partes.append("mensagem enviada com " + ", ".join(f"«{t}»" for t in espera.mensagem_tem))
    if espera.etiqueta:
        partes.append(f"etiqueta {espera.etiqueta}")
    return "; ".join(partes) or "sem expectativa"


def _registros(obtido: Obtido) -> str:
    por_tipo: dict[str, list[Any]] = {}
    for registro in obtido.registros_chatwoot:
        por_tipo.setdefault(registro.tipo, []).append(registro.conteudo)
    if not por_tipo:
        return "nada"
    partes = []
    for tipo, conteudos in por_tipo.items():
        if tipo == "mensagem":
            partes.append(f"{len(conteudos)} mensagem(ns)")
        elif tipo == "nota":
            partes.append("notas: " + " | ".join(_curto(c, 120) for c in conteudos))
        elif tipo == "etiquetas":
            partes.append("etiquetas: " + ", ".join(conteudos[-1]))
        else:
            partes.append(f"{tipo}: " + ", ".join(_curto(c, 80) for c in conteudos))
    return "; ".join(partes)


def _linhas_do_passo(resultado: ResultadoPasso) -> list[str]:
    passo = resultado.passo
    linhas = [
        f"#### Passo {resultado.indice}: {resultado.veredito.value}",
        "",
        f"- **Mensagem:** `{passo.cliente or f'[{passo.anexo} sem texto]'}`",
        f"- **Esperado:** {_esperado(passo)}" + (" (a decidir)" if passo.a_decidir else ""),
    ]
    obtido = resultado.obtido
    if obtido is None:
        if not resultado.esperou_turno:
            linhas.append("- **Obtido:** o turno não foi esperado (a próxima mensagem não esperou)")
        for divergencia in resultado.divergencias:
            linhas.append(f"- **Problema:** {divergencia}")
        return [*linhas, ""]
    reprovadas = (
        "; ".join(
            f"tentativa {n}: {', '.join(r) or 'nenhuma'}"
            for n, r in enumerate(obtido.reprovadas, start=1)
        )
        or "sem redação"
    )
    linhas += [
        f"- **Obtido:** ação {obtido.acao_principal}; anexos {obtido.acoes_anexadas or 'nenhum'}; "
        f"resultado {obtido.resultado}",
        f"- **Reprovadas na prévia:** {reprovadas}",
        f"- **Conversa:** estado {obtido.estado}, motivo {obtido.motivo}",
        f"- **Chatwoot falso:** {_registros(obtido)}",
        f"- **Tempo:** {obtido.segundos:.1f} s",
    ]
    for divergencia in resultado.divergencias:
        linhas.append(f"- **Divergência:** {divergencia}")
    for erro in obtido.erros_no_log:
        linhas.append(f"- **No log:** `{erro}`")
    for aviso in obtido.avisos:
        linhas.append(f"- **Aviso:** `{aviso}`")
    if obtido.texto_enviado:
        linhas += ["- **Texto enviado:**", ""]
        linhas += [f"  > {linha}" for linha in obtido.texto_enviado.splitlines()]
    return [*linhas, ""]


def formatar_resumo(resultados: list[ResultadoRoteiro]) -> str:
    """Uma linha por roteiro, para o terminal."""
    linhas = []
    for numero, r in enumerate(resultados, start=1):
        extras = [
            f"{contagem} {nome}"
            for nome, veredito in (
                ("diverg.", Veredito.DIVERGIU),
                ("erro", Veredito.ERRO),
                ("sem turno", Veredito.SEM_TURNO),
                ("a decidir", Veredito.A_DECIDIR),
            )
            if (contagem := r.vereditos.count(veredito))
        ]
        sufixo = f"  ({', '.join(extras)})" if extras else ""
        linhas.append(f"{numero:>2}. {r.roteiro.nome}: {r.ok}/{r.total} OK{sufixo}")
    return "\n".join(linhas)


def formatar_relatorio(resultados: list[ResultadoRoteiro], quando: str = "") -> str:
    """O relatório em Markdown: o resumo por roteiro e, por passo, o esperado e o obtido."""
    linhas = [f"# Ensaio automático da demo{f' ({quando})' if quando else ''}", ""]
    linhas += ["| # | Roteiro | OK/total | Observação |", "|---|---|---|---|"]
    for numero, r in enumerate(resultados, start=1):
        observacoes = ", ".join(
            f"{r.vereditos.count(v)} {v.value}"
            for v in Veredito
            if v is not Veredito.OK and v in r.vereditos
        )
        if r.interrompido:
            observacoes = f"INTERROMPIDO: {r.interrompido}"
        linhas.append(f"| {numero} | {r.roteiro.nome} | {r.ok}/{r.total} | {observacoes} |")
    linhas.append("")
    for numero, r in enumerate(resultados, start=1):
        linhas += [f"## {numero}. {r.roteiro.nome}: {r.ok}/{r.total} OK", ""]
        linhas.append(f"Telefone inventado {r.telefone}; régua {r.roteiro.regua.value}.")
        linhas.append("")
        for passo in r.passos:
            linhas += _linhas_do_passo(passo)
        if r.fim is not None and r.roteiro.fim is not None:
            esperado = ", ".join(
                f"{nome} {valor if isinstance(valor, int) else valor.value}"
                for nome, valor in (
                    ("estado", r.roteiro.fim.estado),
                    ("motivo", r.roteiro.fim.motivo),
                    ("cobrança", r.roteiro.fim.cobranca),
                    ("mensalidades", r.roteiro.fim.mensalidades),
                )
                if valor is not None
            )
            linhas += [
                f"#### Fim: {r.fim.veredito.value}",
                "",
                f"- **Esperado:** {esperado}",
                f"- **Obtido:** estado {r.fim.estado}, motivo {r.fim.motivo}, cobrança "
                f"{r.fim.cobranca}, mensalidades {r.fim.mensalidades}",
            ]
            linhas += [f"- **Divergência:** {d}" for d in r.fim.divergencias]
            linhas.append("")
    return "\n".join(linhas)


# --------------------------------------------------------------------------- ensaio


def _porta_ocupada(porta: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conexao:
        conexao.settimeout(1)
        return conexao.connect_ex(("127.0.0.1", porta)) == 0


def _chave_do_luna(segredos: Path) -> dict:
    """A chave do Luna: a variável `AGENTE_LLM_API_KEY`, se houver (o servidor Linux, sem a pasta de
    segredos), ou o `openai.local.json` da pasta de segredos."""
    chave = os.environ.get("AGENTE_LLM_API_KEY", "").strip()
    if chave:
        return {"api_key": chave}
    return subir.ler_json(segredos / subir.ARQUIVO_OPENAI)


def ensaiar(
    kit_demo: Path,
    segredos: Path,
    roteiros: list[Roteiro],
    saida: Path,
    porta: int,
    teste: Path | None = None,
) -> int:
    """Sobe o Chatwoot falso e o serviço, roda os roteiros e grava o `relatorio.md` em `saida`.

    Com `teste` (a política do modo teste), o ensaio usa essa política e o cardápio dela, sem
    carteira. Devolve 0 se nenhum passo divergiu, deu erro ou ficou sem turno (o "a decidir" não
    conta), e 1 se algum deu. O Chatwoot falso e o uvicorn são encerrados no `finally`.
    """
    if saida.exists():
        raise ErroDaDemo(f"a pasta {saida} já existe: o ensaio não sobrescreve")
    if _porta_ocupada(porta):
        raise ErroDaDemo(f"a porta {porta} está ocupada: a demo está rodando? Pare-a antes")
    openai = _chave_do_luna(segredos)

    falso = ChatwootFalsoLocal(CONTA)
    processos: list[subprocess.Popen] = []
    resultados: list[ResultadoRoteiro] = []
    quando = datetime.now().strftime("%Y-%m-%d %H:%M")
    falso.iniciar()
    try:
        telefones = montar_kit(kit_demo, saida, roteiros, teste)
        segredo = secrets.token_urlsafe(24)
        ambiente = subir.montar_ambiente(
            chatwoot={
                "url": falso.url,
                "conta": CONTA,
                "inbox": INBOX,
                "token": "token-do-usuario-do-ensaio",
                "token_bot": "token-do-bot-do-ensaio",
            },
            openai=openai,
            kit=saida,
            segredo_webhook=segredo,
            ambiente_base=os.environ,
        )
        # Sem buffer: o ERROR do serviço tem de chegar ao log enquanto o passo ainda roda.
        ambiente["PYTHONUNBUFFERED"] = "1"
        subir.preparar_banco(
            saida, ambiente, None if teste is not None else saida / "carteira-ensaio.csv"
        )

        with (saida / "servico.log").open("wb") as log_do_servico:
            servico = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "agente.api.principal:montar_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(porta),
                ],
                cwd=REPO,
                env=ambiente,
                stdout=log_do_servico,
                stderr=subprocess.STDOUT,
            )
        processos.append(servico)
        subir._esperar(
            lambda: subir._servico_responde(porta), "o serviço responder em /saude", servico
        )

        rodada = Rodada(
            url_webhook=f"http://127.0.0.1:{porta}{subir.CAMINHO_DO_WEBHOOK}{segredo}",
            banco=saida / "demo.db",
            log=saida / "servico.log",
            falso=falso,
            servico_vivo=lambda: servico.poll() is None,
        )
        for numero, roteiro in enumerate(roteiros, start=1):
            print(f"roteiro {numero}/{len(roteiros)}: {roteiro.nome}", flush=True)
            resultados.append(rodar_roteiro(roteiro, numero, telefones[roteiro.nome], rodada))
    finally:
        subir._encerrar(processos)
        falso.parar()
        if resultados:
            (saida / "relatorio.md").write_text(
                formatar_relatorio(resultados, quando), encoding="utf-8"
            )
    print(formatar_resumo(resultados))
    print(f"relatório: {saida / 'relatorio.md'}")
    problemas = {Veredito.DIVERGIU, Veredito.ERRO, Veredito.SEM_TURNO}
    return 1 if any(v in problemas for r in resultados for v in r.vereditos) else 0


def main(argv: list[str] | None = None) -> int:
    for saida_padrao in (sys.stdout, sys.stderr):
        if hasattr(saida_padrao, "reconfigure"):
            saida_padrao.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Ensaio automático da demo no WhatsApp.")
    parser.add_argument("--kit", type=Path, required=True, help="pasta do kit da demo")
    parser.add_argument("--roteiro", help="o nome de um roteiro (o padrão é rodar todos)")
    parser.add_argument(
        "--roteiros", type=Path, default=ROTEIROS_PADRAO, help="o YAML dos roteiros"
    )
    parser.add_argument(
        "--segredos",
        type=Path,
        default=REPO.parent / "secrets",
        help="pasta dos *.local.json (padrão: ../secrets, ao lado do repo)",
    )
    parser.add_argument("--porta", type=int, default=8010)
    parser.add_argument(
        "--teste",
        type=Path,
        help="a política do modo teste (D56): usa o `[teste]` e o cardápio dela, sem carteira",
    )
    argumentos = parser.parse_args(argv)
    try:
        roteiros = carregar_roteiros(argumentos.roteiros)
        if argumentos.roteiro:
            roteiros = [r for r in roteiros if r.nome == argumentos.roteiro]
            if not roteiros:
                raise ErroDaDemo(f"não há roteiro chamado {argumentos.roteiro}")
        kit = argumentos.kit.resolve()
        saida = kit / f"ensaio-{datetime.now().strftime('%Y%m%d-%H%M')}"
        return ensaiar(
            kit,
            argumentos.segredos.resolve(),
            roteiros,
            saida,
            argumentos.porta,
            argumentos.teste.resolve() if argumentos.teste else None,
        )
    except ErroDaDemo as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ensaio interrompido", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
