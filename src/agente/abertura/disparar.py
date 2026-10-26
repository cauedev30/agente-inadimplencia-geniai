"""Dispara a mensagem de abertura da campanha (D3), com o botão "Pagar com Pix" (D26).

A abertura é template e não conta no teto (D33). Cada cobrança recebe uma só: a que já tem conversa
é pulada, e a que falhou fica sem conversa para a próxima rodada.

Ordem das travas (etapa 6: conversa, cobrança, pagamento): o disparador só insere a conversa e a
mensagem, e cada envio faz o próprio commit. O INSERT pega no máximo `FOR KEY SHARE` na cobrança,
que não conflita com a trava `NO KEY UPDATE` da confirmação e do turno, então não espera por eles
nem fecha ciclo.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cobranca, Conversa
from agente.dominio.dinheiro import formatar_reais
from agente.dominio.janela import dentro_da_janela
from agente.integracoes.disparador import Disparador
from agente.politica import Politica
from agente.tipos import Botao, OrigemMensagem, StatusCobranca

log = logging.getLogger(__name__)

BOTAO_PAGAR_PIX = Botao("resposta", "Pagar com Pix", "PAGAR_PIX")


@dataclass
class RelatorioDisparo:
    enviadas: int = 0
    puladas: int = 0
    falhas: list[tuple[str, str]] = field(default_factory=list)
    fora_da_janela: bool = False


def _json(dados: dict) -> str:
    return json.dumps(dados, ensure_ascii=False)


def _primeiro_nome(nome: str) -> str:
    return nome.split()[0] if nome.strip() else ""


def disparar_aberturas(
    sessao: Session, politica: Politica, disparador: Disparador, campanha: str, agora: datetime
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
    for cobranca in cobrancas:
        if cobranca.conversas:
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
            log.warning("abertura falhou para a cobrança %s: %s", cobranca.id, erro)
            relatorio.falhas.append((cobranca.cliente.matricula_ctn, str(erro)))
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
