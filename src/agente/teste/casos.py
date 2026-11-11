"""Os casos do modo teste: quem é testador, a trava de subida e a ativação de um caso (D56)."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agente.carteira.conferir import conferir_cobranca
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.divida import calcular_divida, classificar_regua
from agente.dominio.telefone import normalizar_telefone, variantes_telefone
from agente.integracoes.ctn_teste import CasoInexistente, CTNDeCasos, matricula_simulada
from agente.politica import Politica
from agente.tipos import StatusCobranca
from agente.turno.liberacao import no_piloto

MOTIVO_TROCA_DE_CASO = "troca_de_caso"


def exigir_piloto_fora_da_carteira(sessao: Session, politica: Politica) -> None:
    """A trava de subida: o testador do piloto não pode ter cobrança real ATIVA.

    Os casos do teste trocam a carteira do testador pelos inventados; uma cobrança real dele (da
    importação) cobraria a pessoa de verdade e misturaria as duas. Só roda com o modo teste; sem
    ele o serviço sobe como sempre. A mensagem não leva telefone nem nome, só a contagem.
    """
    teste = politica.teste
    if teste is None:
        return
    telefones: set[str] = set()
    for telefone in politica.liberacao.telefones:
        telefones |= variantes_telefone(telefone)
    reais = sessao.scalar(
        select(func.count())
        .select_from(Cobranca)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(
            Cliente.telefone.in_(telefones),
            Cobranca.status == StatusCobranca.ATIVA,
            Cobranca.campanha != teste.campanha,
        )
    )
    if reais:
        raise ValueError(
            f"o modo teste não sobe: {reais} cobrança(s) ATIVA(s) fora da campanha "
            f"'{teste.campanha}' em telefone(s) do piloto. Encerre-as ou tire o telefone do piloto"
        )


def _ativas_do_telefone(sessao: Session, telefone: str, campanha: str | None) -> list[Cobranca]:
    """As cobranças ATIVAS do telefone (com e sem o 9); com `campanha`, só as dela."""
    consulta = (
        select(Cobranca)
        .join(Cliente, Cobranca.cliente_id == Cliente.id)
        .where(
            Cliente.telefone.in_(variantes_telefone(telefone) or {telefone}),
            Cobranca.status == StatusCobranca.ATIVA,
        )
        .order_by(Cobranca.id)
    )
    if campanha is not None:
        consulta = consulta.where(Cobranca.campanha == campanha)
    return list(sessao.scalars(consulta))


def conversa_viva_do_testador(
    sessao: Session, telefone: str, campanha: str, fora_a_conversa: int
) -> Conversa | None:
    """A conversa ligada ao Chatwoot da cobrança ATIVA de teste do telefone (a mais recente), sem
    contar `fora_a_conversa`. É onde cai a mensagem que chegou numa conversa que já foi trocada."""
    for cobranca in reversed(_ativas_do_telefone(sessao, telefone, campanha)):
        vivas = [
            c
            for c in cobranca.conversas
            if c.chatwoot_conversa_id is not None and c.id != fora_a_conversa
        ]
        if vivas:
            # `cobranca.conversas` não tem ordem: a mais recente é a de maior id.
            return max(vivas, key=lambda conversa: conversa.id)
    return None


def ativar_caso(
    sessao: Session,
    casos: CTNDeCasos,
    politica: Politica,
    telefone: str,
    numero: int,
    agora: datetime,
    desfazer: list[Callable[[], None]] | None = None,
) -> Cobranca:
    """Põe o testador no caso `numero` do cardápio, desde o começo. Não faz commit.

    O estado do `CTNDeCasos` vive na memória e não volta no rollback do banco: com `desfazer`, o
    que restaura o estado anterior da matrícula é acrescentado à lista, e quem chama o executa se
    desfizer a transação. Se a própria ativação falha, o estado já é restaurado aqui.

    Encerra a cobrança ATIVA de teste dele (a troca de caso), cria ou atualiza o `Cliente` da
    matrícula simulada e cria uma cobrança nova, que a conferência de sempre grava (meses, dívida e
    propensão) lendo o `CTNDeCasos`. O mesmo caso de novo é uma cobrança nova do mesmo cliente.
    Número fora do cardápio levanta `CasoInexistente`, sem gravar nada.
    """
    teste = politica.teste
    if teste is None:
        raise ValueError("o modo teste não está ligado no politica.toml")
    caso = next((c for c in teste.casos if c.numero == numero), None)
    if caso is None:
        raise CasoInexistente(str(numero))
    telefone = normalizar_telefone(telefone) or telefone
    matricula = matricula_simulada(numero, telefone)

    for anterior in _ativas_do_telefone(sessao, telefone, teste.campanha):
        anterior.status = StatusCobranca.ENCERRADA
        anterior.encerrada_motivo = MOTIVO_TROCA_DE_CASO
        anterior.encerrada_em = agora
    # A encerrada tem de sair do índice único (uma ATIVA por cliente) antes de a nova entrar.
    sessao.flush()

    cliente = sessao.scalar(select(Cliente).where(Cliente.matricula_ctn == matricula))
    if cliente is None:
        cliente = Cliente(matricula_ctn=matricula, nome=caso.nome, telefone=telefone)
        sessao.add(cliente)
    cliente.nome = caso.nome
    cliente.telefone = telefone
    anterior = casos.ativar(matricula, telefone)
    try:
        cobranca = Cobranca(
            cliente=cliente,
            regua=classificar_regua(caso.meses_atraso, None, politica),
            mensalidades_abertas=caso.meses_atraso,
            mensalidades_retrato=caso.meses_atraso,
            divida_centavos=calcular_divida(caso.meses_atraso, politica),
            propenso_cartao=False,
            campanha=teste.campanha,
            ult_mesref=casos.ult_mesref(matricula),
        )
        sessao.add(cobranca)
        sessao.flush()
        # Grava `conferida_em`, `meses_em_aberto` e a propensão, pelo caminho de sempre.
        conferir_cobranca(sessao, casos, politica, cobranca.id, agora)
    except BaseException:
        casos.restaurar(matricula, anterior)
        raise
    if desfazer is not None:
        desfazer.append(lambda: casos.restaurar(matricula, anterior))
    return cobranca


def garantir_caso(
    sessao: Session, casos: CTNDeCasos, politica: Politica, telefone: str, agora: datetime
) -> Cobranca | None:
    """O 1º contato do testador: o telefone do piloto sem cobrança ATIVA recebe o caso padrão.

    Devolve a cobrança criada, ou None se o telefone não é do piloto ou já tem cobrança ATIVA."""
    if politica.teste is None or not no_piloto(telefone, politica):
        return None
    if _ativas_do_telefone(sessao, telefone, None):
        return None
    return ativar_caso(sessao, casos, politica, telefone, politica.teste.caso_padrao, agora)
