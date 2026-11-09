"""Importa a carteira do dia pelo retrato da inadimplência do CTN (spec do CTN real, seção 4).

O retrato é a lista do dia: quem tem `meses_atraso >= 1` entra (ou é atualizado); quem tem 0 (a
Energisa, que o CTN não conta como atraso) fica de fora; quem tinha cobrança ATIVA e sumiu do
retrato é encerrado, mas só se o retrato veio inteiro e traz ao menos um inadimplente. A régua e a
dívida saem de `dominio/divida.py` (a dívida é `meses_atraso x mensalidade`, D24). A propensão ao
cartão não é calculada aqui: custaria uma chamada por cliente e sai na conferência no contato.

A gravação é por página (`PAGINA`): se o CTN cair no meio, o que já veio fica gravado e nada é
encerrado, porque quem ainda não apareceu pode estar na parte que faltou.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from agente.carteira.importar import _gravar, _Linha
from agente.db.modelos import Cliente, Cobranca, Pagamento
from agente.integracoes.ctn import CTN, CTNIndisponivel
from agente.politica import Politica
from agente.tipos import StatusCobranca, StatusPagamento

# Linhas por página da API (50): a mesma cadência do commit.
PAGINA = 50

MOTIVO_SAIU_DO_RETRATO = "saiu_do_retrato"
SEM_ENCERRAR_INCOMPLETO = "retrato_incompleto"
SEM_ENCERRAR_VAZIO = "retrato_vazio"


@dataclass
class ResumoImportacao:
    """Os totais da importação; só contagens, nunca dado de cliente.

    `motivo_sem_encerrar` é `None` quando o encerramento rodou. `preservadas` são as cobranças que
    saíram do retrato mas têm um Pix pendente nosso: ficam ATIVAS até o Pix expirar ou ser pago.
    """

    criadas: int = 0
    atualizadas: int = 0
    encerradas: int = 0
    ignoradas_energisa: int = 0
    total_retrato: int = 0
    sem_telefone: int = 0
    preservadas: int = 0
    motivo_sem_encerrar: str | None = None

    @property
    def completo(self) -> bool:
        return self.motivo_sem_encerrar != SEM_ENCERRAR_INCOMPLETO


def _encerrar_quem_saiu(sessao: Session, vistas: set[str], resumo: ResumoImportacao) -> None:
    pendente = exists().where(
        Pagamento.cobranca_id == Cobranca.id, Pagamento.status == StatusPagamento.PENDENTE
    )
    ativas = sessao.execute(
        select(Cobranca, Cliente.matricula_ctn, pendente)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(Cobranca.status == StatusCobranca.ATIVA)
    ).all()
    for cobranca, matricula, com_pix_pendente in ativas:
        if matricula in vistas:
            continue
        if com_pix_pendente:
            resumo.preservadas += 1
            continue
        cobranca.status = StatusCobranca.ENCERRADA
        cobranca.encerrada_motivo = MOTIVO_SAIU_DO_RETRATO
        resumo.encerradas += 1
    sessao.flush()


def importar_do_ctn(
    sessao: Session,
    ctn: CTN,
    politica: Politica,
    data_ref: date,
    campanha: str,
    simular: bool = False,
) -> ResumoImportacao:
    """Importa o retrato de `data_ref`. Com `simular`, faz tudo e desfaz: devolve os totais que a
    execução real teria e não deixa nada gravado."""
    resumo = ResumoImportacao()
    vistas: set[str] = set()
    na_pagina = 0
    try:
        for linha in ctn.inadimplentes(data_ref):
            resumo.total_retrato += 1
            if linha.meses_atraso < 1:
                resumo.ignoradas_energisa += 1
                continue
            if linha.matricula in vistas:
                continue
            vistas.add(linha.matricula)
            if not linha.telefone:
                resumo.sem_telefone += 1
                continue
            criou = _gravar(
                sessao,
                _Linha(
                    matricula=linha.matricula,
                    nome=linha.nome,
                    telefone=linha.telefone,
                    mensalidades_abertas=linha.meses_atraso,
                    regua=None,
                ),
                politica,
                None,
                campanha,
            )
            if criou:
                resumo.criadas += 1
            else:
                resumo.atualizadas += 1
            na_pagina += 1
            if na_pagina >= PAGINA:
                na_pagina = 0
                if not simular:
                    sessao.commit()
    except CTNIndisponivel:
        resumo.motivo_sem_encerrar = SEM_ENCERRAR_INCOMPLETO
    else:
        if not vistas:
            resumo.motivo_sem_encerrar = SEM_ENCERRAR_VAZIO
        else:
            _encerrar_quem_saiu(sessao, vistas, resumo)
    if simular:
        sessao.rollback()
    else:
        sessao.commit()
    return resumo
