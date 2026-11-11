"""Dispara a mensagem de abertura da campanha (D3), com o botão "Pagar com Pix" (D26).

A abertura é template e não conta no teto (D33). Cada cobrança recebe uma só: a que já tem conversa
é pulada, e a que falhou fica sem conversa para a próxima rodada.

Só as cobranças liberadas (`modo_da_conversa` ATIVO: o piloto, a parte ou a carteira toda da
`liberacao` do politica.toml) recebem a abertura; as demais são puladas ANTES da conferência, sem
chamar o CTN, e contam em `fora_da_liberacao`. Em sombra, nada é disparado.

Com o CTN (`ctn`), a cobrança é conferida antes da abertura, uma vez por dia (spec do CTN real,
seção 4): o valor do template é o conferido, e só ele. A que o CTN mostra quitada, a que não tem
lançamentos, a de dado suspeito e a que não foi conferida (CTN fora do ar) NÃO recebem a abertura
(nunca sai valor estimado): ficam ATIVAS e a próxima rodada tenta de novo. A chamada ao CTN vem
antes de qualquer escrita da cobrança, e o commit da conferência solta a trava curta dela antes do
envio.

Parada de segurança: se, numa rodada, mais de `ALARME_CONFERENCIA_PERCENTUAL`% das conferências (com
pelo menos `ALARME_CONFERENCIA_MINIMO`) forem quitada ou sem lançamentos, o CTN ou a carteira está
errado: a rodada para na hora e marca `alarme_conferencia` no relatório.

O telefone que pertence a mais de uma cobrança ativa (também com e sem o 9) não recebe a abertura:
não se sabe a quem ela chegaria. Essas vão à revisão humana (aqui só se registra e conta).

Ordem das travas (etapa 6: conversa, cobrança, pagamento): o disparador só insere a conversa e a
mensagem, e cada envio faz o próprio commit. O INSERT pega no máximo `FOR KEY SHARE` na cobrança,
que não conflita com a trava `NO KEY UPDATE` da confirmação e do turno, então não espera por eles
nem fecha ciclo.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.carteira.conferir import ResultadoConferencia, conferir_antes_do_envio
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.dinheiro import formatar_reais
from agente.dominio.janela import dentro_da_janela
from agente.dominio.telefone import variantes_telefone
from agente.integracoes.ctn import CTN
from agente.integracoes.disparador import Disparador
from agente.politica import Politica
from agente.tipos import Botao, Modo, OrigemMensagem, StatusCobranca
from agente.turno.liberacao import modo_da_conversa

log = logging.getLogger(__name__)

BOTAO_PAGAR_PIX = Botao("resposta", "Pagar com Pix", "PAGAR_PIX")

# A parada de segurança da rodada: com pelo menos este nº de conferências feitas, mais deste
# percentual de "quitada" ou "sem lançamentos" para a campanha (o CTN ou a carteira está errado).
ALARME_CONFERENCIA_MINIMO = 20
ALARME_CONFERENCIA_PERCENTUAL = 30

# O resultado que conta como conferência feita (o CTN respondeu); só o que falhou fica de fora.
_CONFERENCIAS_FEITAS = (
    ResultadoConferencia.CONFERIDA,
    ResultadoConferencia.QUITADA,
    ResultadoConferencia.SEM_LANCAMENTOS,
    ResultadoConferencia.DADO_SUSPEITO,
)


@dataclass
class RelatorioDisparo:
    """As contagens de uma rodada. Cada cobrança cai em um só contador de "não saiu"."""

    enviadas: int = 0
    puladas: int = 0
    # A conferência no CTN achou a cobrança quitada (havia rejeitadas e todas foram pagas).
    quitadas_no_ctn: int = 0
    # O CTN não devolveu lançamento nenhum, mas o retrato diz que há meses em atraso.
    sem_lancamentos: int = 0
    # Mês repetido, valor zero, negativo ou fora do teto, ou meses demais.
    dados_suspeitos: int = 0
    # A conferência não foi feita (CTN fora do ar): sem valor estimado, fica para a próxima rodada.
    nao_conferidas: int = 0
    # O telefone é de mais de uma cobrança ativa: vai à revisão humana.
    telefone_compartilhado: int = 0
    # A cobrança não está liberada para o modo ativo (fora do piloto ou da parte liberada): não
    # recebe a abertura, e o CTN nem é consultado.
    fora_da_liberacao: int = 0
    # A rodada parou porque quitadas + sem lançamentos passaram do limite das conferências.
    alarme_conferencia: bool = False
    falhas: list[tuple[str, str]] = field(default_factory=list)
    fora_da_janela: bool = False


def _json(dados: dict) -> str:
    return json.dumps(dados, ensure_ascii=False)


def _com_telefone_compartilhado(sessao: Session) -> set[int]:
    """Os ids das cobranças ativas cujo telefone (ou variante, A1) é de outra cobrança ativa."""
    linhas = sessao.execute(
        select(Cobranca.id, Cliente.telefone)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(Cobranca.status == StatusCobranca.ATIVA)
    ).all()
    donos: dict[str, set[int]] = defaultdict(set)
    for cobranca_id, telefone in linhas:
        for variante in variantes_telefone(telefone):
            donos[variante].add(cobranca_id)
    return {cobranca_id for ids in donos.values() if len(ids) > 1 for cobranca_id in ids}


def _passou_do_limite(quitadas_ou_sem: int, conferencias: int) -> bool:
    return (
        conferencias >= ALARME_CONFERENCIA_MINIMO
        and quitadas_ou_sem * 100 > ALARME_CONFERENCIA_PERCENTUAL * conferencias
    )


def _primeiro_nome(nome: str) -> str:
    return nome.split()[0] if nome.strip() else ""


def disparar_aberturas(
    sessao: Session,
    politica: Politica,
    disparador: Disparador,
    campanha: str,
    agora: datetime,
    ctn: CTN | None = None,
) -> RelatorioDisparo:
    relatorio = RelatorioDisparo()
    if not dentro_da_janela(agora, politica):
        relatorio.fora_da_janela = True
        return relatorio

    cobrancas = sessao.scalars(
        select(Cobranca)
        .where(Cobranca.campanha == campanha, Cobranca.status == StatusCobranca.ATIVA)
        .order_by(Cobranca.id)
    ).all()
    compartilhadas = _com_telefone_compartilhado(sessao)
    conferencias = 0
    for cobranca in cobrancas:
        if cobranca.conversas:
            relatorio.puladas += 1
            continue
        if modo_da_conversa(cobranca, politica) is not Modo.ATIVO:
            relatorio.fora_da_liberacao += 1
            continue
        if cobranca.id in compartilhadas:
            log.warning(
                "a cobrança %s tem telefone de outra cobrança ativa; fica sem abertura", cobranca.id
            )
            relatorio.telefone_compartilhado += 1
            continue
        if ctn is not None:
            resultado = conferir_antes_do_envio(sessao, ctn, politica, cobranca.id, agora)
            if resultado is ResultadoConferencia.QUITADA:
                relatorio.quitadas_no_ctn += 1
            elif resultado is ResultadoConferencia.SEM_LANCAMENTOS:
                relatorio.sem_lancamentos += 1
            elif resultado is ResultadoConferencia.DADO_SUSPEITO:
                relatorio.dados_suspeitos += 1
            elif resultado is ResultadoConferencia.NAO_CONFERIDA:
                relatorio.nao_conferidas += 1
            if resultado in _CONFERENCIAS_FEITAS:
                conferencias += 1
            if _passou_do_limite(
                relatorio.quitadas_no_ctn + relatorio.sem_lancamentos, conferencias
            ):
                # Nada mais sai nesta rodada, nem esta cobrança: a carteira pede revisão.
                log.error(
                    "alarme da conferência: %s de %s conferências quitadas ou sem lançamentos; "
                    "a rodada parou",
                    relatorio.quitadas_no_ctn + relatorio.sem_lancamentos,
                    conferencias,
                )
                relatorio.alarme_conferencia = True
                break
            if resultado is not ResultadoConferencia.CONFERIDA:
                # Sem conferência de hoje o valor é estimado, e a quitada não deve nada: não sai.
                continue
            # A chamada ao CTN pode ter durado mais de um minuto: a cobrança pode ter fechado ou
            # ganhado conversa (o turno, outro disparo) nesse tempo.
            sessao.refresh(cobranca)
            if cobranca.status is not StatusCobranca.ATIVA or cobranca.conversas:
                relatorio.puladas += 1
                continue
        variaveis = {
            "nome": _primeiro_nome(cobranca.cliente.nome),
            "valor": formatar_reais(cobranca.divida_centavos),
        }
        try:
            disparador.enviar_template(
                cobranca.cliente.telefone, politica.template_abertura, variaveis, [BOTAO_PAGAR_PIX]
            )
        except Exception as erro:  # um cliente que falha não para a campanha
            log.warning("abertura falhou para a cobrança %s: %s", cobranca.id, type(erro).__name__)
            # Só o tipo: a mensagem do erro pode trazer telefone, nome ou URL.
            relatorio.falhas.append((cobranca.cliente.matricula_ctn, type(erro).__name__))
            continue
        conversa = Conversa(cobranca=cobranca)
        sessao.add(conversa)
        sessao.flush()
        registrar_mensagem(
            sessao,
            conversa_id=conversa.id,
            origem=OrigemMensagem.ABERTURA,
            texto=f"[template {politica.template_abertura}] {_json(variaveis)}",
            chatwoot_message_id=None,
            conta_no_teto=False,
            momento=agora,
        )
        # Commit a cada envio: se o processo cair no meio, ninguém recebe a abertura duas vezes.
        sessao.commit()
        relatorio.enviadas += 1
    return relatorio
