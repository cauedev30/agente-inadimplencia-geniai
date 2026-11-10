"""Importa a carteira de um CSV (D2): cliente, régua, dívida e propensão ao cartão.

Ordem das travas (etapa 6: conversa, cobrança, pagamento): a importação só escreve em cliente e
cobrança e nunca toca em conversa nem em pagamento. Por isso espera pela confirmação e pelo turno,
que seguram a cobrança, mas não fecha ciclo com eles: quem segura a cobrança dela não espera
conversa nenhuma. A transação é uma só, então as cobranças já atualizadas ficam travadas até o fim
do arquivo, e uma confirmação de pagamento dessas cobranças espera o commit.
"""

import csv
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Cliente, Cobranca
from agente.dominio.divida import calcular_divida, classificar_regua
from agente.dominio.propensao import eh_propenso
from agente.dominio.telefone import normalizar_telefone
from agente.integracoes.ctn import CTN
from agente.politica import Politica, Regua
from agente.tipos import StatusCobranca

# O motivo da cobrança que a importação do CTN encerra por ter saído do retrato, e quantos dias
# depois (no fuso da janela) ela ainda é reativada se voltar, em vez de nascer uma nova.
MOTIVO_SAIU_DO_RETRATO = "saiu_do_retrato"
DIAS_PARA_REATIVAR = 3

ResultadoGravacao = Literal["criada", "atualizada", "reativada"]


@dataclass
class RelatorioImportacao:
    criadas: int = 0
    atualizadas: int = 0
    rejeitadas: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Linha:
    matricula: str
    nome: str
    telefone: str
    mensalidades_abertas: int
    regua: Regua | None
    # O `ult_mesref` do retrato do CTN; o CSV não tem essa informação.
    ult_mesref: date | None = None


class _LinhaInvalida(ValueError):
    pass


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _ler_regua(bruto: str) -> Regua | None:
    texto = _sem_acento(bruto.strip()).upper()
    if not texto:
        return None
    if texto.startswith("PREVIA"):
        return Regua.PREVIA
    try:
        return Regua(texto)
    except ValueError:
        raise _LinhaInvalida(f"régua desconhecida: {bruto}") from None


def _ler_linha(registro: dict[str, str], politica: Politica) -> _Linha:
    colunas = politica.carteira.colunas
    matricula = (registro.get(colunas.matricula) or "").strip()
    if not matricula:
        raise _LinhaInvalida("matrícula vazia")
    telefone = normalizar_telefone(registro.get(colunas.telefone) or "")
    if telefone is None:
        raise _LinhaInvalida(f"telefone inválido: {registro.get(colunas.telefone)!r}")
    try:
        mensalidades = int((registro.get(colunas.mensalidades_abertas) or "").strip())
    except ValueError:
        raise _LinhaInvalida(
            f"{colunas.mensalidades_abertas} não é um número: "
            f"{registro.get(colunas.mensalidades_abertas)!r}"
        ) from None
    if mensalidades < 1:
        raise _LinhaInvalida(f"sem mensalidade em aberto: {mensalidades}")
    return _Linha(
        matricula=matricula,
        nome=(registro.get(colunas.nome) or "").strip(),
        telefone=telefone,
        mensalidades_abertas=mensalidades,
        regua=_ler_regua(registro.get(colunas.regua) or ""),
    )


def _propenso(ctn: CTN, matricula: str, politica: Politica) -> bool:
    # Um mês a mais, porque o mês corrente pode não ter pagamento ainda. Matrícula desconhecida
    # volta como histórico vazio (contrato do CTN), e sem histórico o cliente não é propenso.
    historico = ctn.historico_pagamentos(matricula, meses=politica.propensao.n_meses + 1)
    return eh_propenso(historico, politica)


def _dia(momento: datetime, politica: Politica):
    return momento.astimezone(politica.janela.zona).date()


def _recem_encerrada(
    sessao: Session, cliente: Cliente, campanha: str, politica: Politica, agora: datetime
) -> Cobranca | None:
    """A cobrança que a importação encerrou por `saiu_do_retrato` há no máximo
    `DIAS_PARA_REATIVAR` dias (no fuso da janela), da mesma campanha. A encerrada por outro motivo
    e a PAGA nunca voltam."""
    recente = sessao.scalar(
        select(Cobranca)
        .where(
            Cobranca.cliente_id == cliente.id,
            Cobranca.status == StatusCobranca.ENCERRADA,
            Cobranca.encerrada_motivo == MOTIVO_SAIU_DO_RETRATO,
            Cobranca.campanha == campanha,
            Cobranca.encerrada_em.is_not(None),
        )
        .order_by(Cobranca.encerrada_em.desc())
        .limit(1)
    )
    if recente is None:
        return None
    if (_dia(agora, politica) - _dia(recente.encerrada_em, politica)).days > DIAS_PARA_REATIVAR:
        return None
    return recente


def _gravar(
    sessao: Session,
    linha: _Linha,
    politica: Politica,
    ctn: CTN | None,
    campanha: str,
    agora: datetime | None = None,
) -> ResultadoGravacao:
    """Grava a linha. Devolve "criada", "atualizada" (a ativa) ou "reativada".

    Com `ctn=None` a propensão não é calculada: a cobrança nova nasce com `propenso_cartao` falso
    e a ativa mantém o valor que tinha (a importação do CTN deixa a propensão para a conferência).

    Com `agora` (a importação do CTN): a ativa já CONFERIDA HOJE (dia do `agora` no fuso da janela)
    mantém a régua, a dívida e as mensalidades que a conferência leu, porque o retrato é só uma
    estimativa; e, sem ativa, a cobrança que saiu do retrato há até `DIAS_PARA_REATIVAR` dias é
    reativada (mantém a conversa) em vez de nascer outra.
    """
    cliente = sessao.scalar(select(Cliente).where(Cliente.matricula_ctn == linha.matricula))
    if cliente is None:
        cliente = Cliente(matricula_ctn=linha.matricula, nome=linha.nome, telefone=linha.telefone)
        sessao.add(cliente)
    else:
        cliente.nome = linha.nome or cliente.nome
        cliente.telefone = linha.telefone

    valores = {
        "regua": classificar_regua(linha.mensalidades_abertas, linha.regua, politica),
        "mensalidades_abertas": linha.mensalidades_abertas,
        "mensalidades_retrato": linha.mensalidades_abertas,
        "ult_mesref": linha.ult_mesref,
        "divida_centavos": calcular_divida(linha.mensalidades_abertas, politica),
        "campanha": campanha,
    }
    if ctn is not None:
        valores["propenso_cartao"] = _propenso(ctn, linha.matricula, politica)
    ativa = None
    reativada = False
    if cliente.id is not None:
        ativa = sessao.scalar(
            select(Cobranca).where(
                Cobranca.cliente_id == cliente.id, Cobranca.status == StatusCobranca.ATIVA
            )
        )
        if ativa is None and agora is not None:
            ativa = _recem_encerrada(sessao, cliente, campanha, politica, agora)
            if ativa is not None:
                ativa.status = StatusCobranca.ATIVA
                ativa.encerrada_motivo = None
                ativa.encerrada_em = None
                reativada = True
    if ativa is None:
        valores.setdefault("propenso_cartao", False)
        sessao.add(Cobranca(cliente=cliente, **valores))
        sessao.flush()
        return "criada"
    if (
        agora is not None
        and ativa.conferida_em is not None
        and _dia(ativa.conferida_em, politica) == _dia(agora, politica)
    ):
        for nome in ("regua", "mensalidades_abertas", "divida_centavos"):
            del valores[nome]
    for nome, valor in valores.items():
        setattr(ativa, nome, valor)
    sessao.flush()
    return "reativada" if reativada else "atualizada"


def importar_carteira(
    caminho: Path, sessao: Session, politica: Politica, ctn: CTN, campanha: str
) -> RelatorioImportacao:
    """Importa o CSV numa transação só. A linha inválida é rejeitada com o número dela."""
    colunas = politica.carteira.colunas
    obrigatorias = [colunas.matricula, colunas.nome, colunas.telefone, colunas.mensalidades_abertas]
    relatorio = RelatorioImportacao()
    with open(caminho, encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo, delimiter=politica.carteira.separador)
        faltando = [nome for nome in obrigatorias if nome not in (leitor.fieldnames or [])]
        if faltando:
            raise ValueError(f"{caminho}: faltam as colunas {', '.join(faltando)}")
        for registro in leitor:
            numero = leitor.line_num
            try:
                linha = _ler_linha(registro, politica)
            except _LinhaInvalida as erro:
                relatorio.rejeitadas.append((numero, str(erro)))
                continue
            if _gravar(sessao, linha, politica, ctn, campanha) == "criada":
                relatorio.criadas += 1
            else:
                relatorio.atualizadas += 1
    sessao.commit()
    return relatorio
