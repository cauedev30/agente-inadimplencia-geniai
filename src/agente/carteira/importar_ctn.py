"""Importa a carteira do dia pelo retrato da inadimplência do CTN (spec do CTN real, seção 4).

O retrato é a lista do dia: quem tem `meses_atraso >= 1` entra (ou é atualizado); quem tem 0 (a
Energisa, que o CTN não conta como atraso) fica de fora; quem tinha cobrança ATIVA da mesma campanha
e sumiu do retrato é encerrado, mas só se o retrato veio inteiro e confiável. A régua e a dívida
saem de `dominio/divida.py` (a dívida é `meses_atraso x mensalidade`, D24). A propensão ao cartão
não é calculada aqui: custaria uma chamada por cliente e sai na conferência no contato.

Ordem: primeiro o retrato INTEIRO é lido para a memória (a espera do limite de taxa nunca acontece
com UPDATE pendente); se a leitura falhar no meio, nada é gravado nem encerrado. Só então grava, em
lotes de `PAGINA` com commit por lote, e por fim encerra. O retrato é suspeito (não encerra) quando
traz linha com `data_ref` diferente da pedida, ou nenhuma linha com ela; e o encerramento que
atingiria mais de `TETO_DO_ENCERRAMENTO` das ativas da campanha também não roda.

Versão de teste (D56, P6): `meses_minimos` deixa de fora quem tem menos meses de atraso (a
importação diária do `dev2` usa 3: só R3 a R6), a matrícula simulada `SIM-` nunca entra, e o
telefone de `telefones_excluidos` (os testadores do piloto, com e sem o 9) também não. Tudo o que
fica de fora conta como fora do retrato: a ATIVA dessa pessoa é encerrada pelas regras de sempre
(o teto de 25% vale) e nenhuma é criada nem atualizada.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from agente.carteira.importar import (
    DIAS_PARA_REATIVAR,
    MOTIVO_SAIU_DO_RETRATO,
    _gravar,
    _Linha,
)
from agente.db.modelos import Cliente, Cobranca, Pagamento
from agente.dominio.telefone import variantes_telefone
from agente.integracoes.ctn import CTN, CTNIndisponivel, InadimplenteCTN
from agente.integracoes.ctn_teste import PREFIXO_SIMULADO
from agente.politica import Politica
from agente.tipos import StatusCobranca, StatusPagamento

__all__ = [
    "DIAS_PARA_REATIVAR",
    "MOTIVO_SAIU_DO_RETRATO",
    "ResumoImportacao",
    "importar_do_ctn",
]

# Linhas por lote de gravação (a mesma cadência da página da API): um commit por lote.
PAGINA = 50

SEM_ENCERRAR_INCOMPLETO = "retrato_incompleto"
SEM_ENCERRAR_VAZIO = "retrato_vazio"
SEM_ENCERRAR_SUSPEITO = "retrato_suspeito"
SEM_ENCERRAR_EXCESSIVO = "encerramento_excessivo"

# O encerramento que atingiria mais que esta fração das ativas da campanha não roda (um retrato
# cortado ou trocado pareceria "todo mundo pagou"), mas só vale com ao menos este nº de ativas.
TETO_DO_ENCERRAMENTO = 0.25
MINIMO_DE_ATIVAS_PARA_O_TETO = 20
# O UPDATE do encerramento vai em blocos (o limite de variáveis do SQLite).
BLOCO_DO_ENCERRAMENTO = 500


@dataclass
class ResumoImportacao:
    """Os totais da importação; só contagens, nunca dado de cliente.

    `motivo_sem_encerrar` é `None` quando o encerramento rodou. `preservadas` são as cobranças que
    saíram do retrato mas têm um Pix pendente nosso: ficam ATIVAS até o Pix expirar ou ser pago.
    `inadimplentes` são as matrículas distintas com 1 mês ou mais de atraso na data pedida.
    `reativadas` voltaram ao retrato até `DIAS_PARA_REATIVAR` dias depois de encerradas.
    `linhas_invalidas` são as malformadas do retrato; `data_ref_divergente`, as de outro dia;
    `telefones_compartilhados`, os telefones que aparecem em mais de uma matrícula inadimplente.
    `abaixo_do_minimo` (menos meses que `meses_minimos`), `ignoradas_simuladas` (matrícula `SIM-`)
    e `ignoradas_piloto` (telefone de testador) são as que a versão de teste deixa de fora;
    `inadimplentes` conta só as que entram.
    """

    criadas: int = 0
    atualizadas: int = 0
    reativadas: int = 0
    encerradas: int = 0
    ignoradas_energisa: int = 0
    total_retrato: int = 0
    inadimplentes: int = 0
    sem_telefone: int = 0
    preservadas: int = 0
    linhas_invalidas: int = 0
    data_ref_divergente: int = 0
    telefones_compartilhados: int = 0
    abaixo_do_minimo: int = 0
    ignoradas_simuladas: int = 0
    ignoradas_piloto: int = 0
    motivo_sem_encerrar: str | None = None

    @property
    def completo(self) -> bool:
        return self.motivo_sem_encerrar != SEM_ENCERRAR_INCOMPLETO


@dataclass
class _Retrato:
    """O retrato lido e classificado, sem nada gravado."""

    a_gravar: list[InadimplenteCTN]
    vistas: set[str]


def _ler_o_retrato(
    ctn: CTN,
    data_ref: date,
    resumo: ResumoImportacao,
    meses_minimos: int,
    excluidos: frozenset[str],
) -> _Retrato:
    """Lê o retrato inteiro (levanta CTNIndisponivel se cair no meio) e o classifica."""
    invalidas: list[str | None] = []
    linhas = list(ctn.inadimplentes(data_ref, invalidas))
    resumo.linhas_invalidas = len(invalidas)
    # A linha malformada de matrícula legível conta como vista: quem continua no retrato não é
    # encerrado por um defeito de formato.
    vistas = {matricula for matricula in invalidas if matricula}
    a_gravar: list[InadimplenteCTN] = []
    gravadas: set[str] = set()
    com_a_data_pedida = 0
    resumo.total_retrato = len(linhas) + len(invalidas)
    for linha in linhas:
        if linha.data_ref != data_ref:
            resumo.data_ref_divergente += 1
            continue
        com_a_data_pedida += 1
        if linha.meses_atraso < 1:
            resumo.ignoradas_energisa += 1
            continue
        if linha.matricula.startswith(PREFIXO_SIMULADO):
            resumo.ignoradas_simuladas += 1
            continue
        if excluidos and not excluidos.isdisjoint(variantes_telefone(linha.telefone or "")):
            resumo.ignoradas_piloto += 1
            continue
        if linha.meses_atraso < meses_minimos:
            resumo.abaixo_do_minimo += 1
            continue
        if linha.matricula in gravadas:
            continue
        gravadas.add(linha.matricula)
        vistas.add(linha.matricula)
        a_gravar.append(linha)
    resumo.inadimplentes = len(a_gravar)
    resumo.sem_telefone = sum(1 for linha in a_gravar if not linha.telefone)
    donos: dict[str, int] = {}
    for linha in a_gravar:
        if linha.telefone:
            donos[linha.telefone] = donos.get(linha.telefone, 0) + 1
    resumo.telefones_compartilhados = sum(1 for n in donos.values() if n > 1)
    if resumo.data_ref_divergente or (resumo.total_retrato and not com_a_data_pedida):
        resumo.motivo_sem_encerrar = SEM_ENCERRAR_SUSPEITO
    elif not vistas:
        resumo.motivo_sem_encerrar = SEM_ENCERRAR_VAZIO
    return _Retrato(a_gravar, vistas)


def _ativas_da_campanha(sessao: Session, campanha: str):
    pendente = exists().where(
        Pagamento.cobranca_id == Cobranca.id, Pagamento.status == StatusPagamento.PENDENTE
    )
    return sessao.execute(
        select(Cobranca.id, Cliente.matricula_ctn, pendente)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(Cobranca.status == StatusCobranca.ATIVA, Cobranca.campanha == campanha)
    ).all()


def _encerrar_quem_saiu(
    sessao: Session,
    ativas: list,
    vistas: set[str],
    agora: datetime,
    resumo: ResumoImportacao,
) -> None:
    """Encerra as `ativas` (lidas antes da gravação, só da campanha) que não estão em `vistas`. O
    UPDATE é condicional no mesmo statement (ainda ATIVA e sem Pix pendente), então uma cobrança
    que pagou ou ganhou um Pix entre a leitura e a escrita não é encerrada."""
    a_encerrar = []
    for cobranca_id, matricula, com_pix_pendente in ativas:
        if matricula in vistas:
            continue
        if com_pix_pendente:
            resumo.preservadas += 1
            continue
        a_encerrar.append(cobranca_id)
    if (
        len(ativas) >= MINIMO_DE_ATIVAS_PARA_O_TETO
        and len(a_encerrar) > len(ativas) * TETO_DO_ENCERRAMENTO
    ):
        resumo.motivo_sem_encerrar = SEM_ENCERRAR_EXCESSIVO
        resumo.preservadas = 0
        return
    pendente = exists().where(
        Pagamento.cobranca_id == Cobranca.id, Pagamento.status == StatusPagamento.PENDENTE
    )
    for inicio in range(0, len(a_encerrar), BLOCO_DO_ENCERRAMENTO):
        bloco = a_encerrar[inicio : inicio + BLOCO_DO_ENCERRAMENTO]
        resultado = sessao.execute(
            update(Cobranca)
            .where(Cobranca.id.in_(bloco), Cobranca.status == StatusCobranca.ATIVA, ~pendente)
            .values(
                status=StatusCobranca.ENCERRADA,
                encerrada_motivo=MOTIVO_SAIU_DO_RETRATO,
                encerrada_em=agora,
            )
            .execution_options(synchronize_session="fetch")
        )
        resumo.encerradas += resultado.rowcount
    sessao.flush()


def importar_do_ctn(
    sessao: Session,
    ctn: CTN,
    politica: Politica,
    data_ref: date,
    campanha: str,
    simular: bool = False,
    agora: datetime | None = None,
    meses_minimos: int = 1,
    telefones_excluidos: frozenset[str] = frozenset(),
) -> ResumoImportacao:
    """Importa o retrato de `data_ref`. Com `simular`, faz tudo e desfaz: devolve os totais que a
    execução real teria e não deixa nada gravado. `meses_minimos` e `telefones_excluidos` são da
    versão de teste (ver o alto do módulo)."""
    if meses_minimos < 1:
        raise ValueError("meses_minimos precisa ser ao menos 1")
    agora = agora or datetime.now(UTC)
    excluidos = frozenset(
        variante for telefone in telefones_excluidos for variante in variantes_telefone(telefone)
    )
    resumo = ResumoImportacao()
    try:
        retrato = _ler_o_retrato(ctn, data_ref, resumo, meses_minimos, excluidos)
    except CTNIndisponivel:
        # Leitura incompleta: nada foi escrito (a leitura vem antes de qualquer UPDATE).
        return ResumoImportacao(motivo_sem_encerrar=SEM_ENCERRAR_INCOMPLETO)

    ativas = _ativas_da_campanha(sessao, campanha)
    for inicio in range(0, len(retrato.a_gravar), PAGINA):
        for linha in retrato.a_gravar[inicio : inicio + PAGINA]:
            if not linha.telefone:
                continue
            resultado = _gravar(
                sessao,
                _Linha(
                    matricula=linha.matricula,
                    nome=linha.nome,
                    telefone=linha.telefone,
                    mensalidades_abertas=linha.meses_atraso,
                    regua=None,
                    ult_mesref=linha.ult_mesref,
                ),
                politica,
                None,
                campanha,
                agora,
            )
            if resultado == "criada":
                resumo.criadas += 1
            elif resultado == "reativada":
                resumo.reativadas += 1
            else:
                resumo.atualizadas += 1
        if not simular:
            sessao.commit()
    if resumo.motivo_sem_encerrar is None:
        _encerrar_quem_saiu(sessao, ativas, retrato.vistas, agora, resumo)
    if simular:
        sessao.rollback()
    else:
        sessao.commit()
    return resumo
