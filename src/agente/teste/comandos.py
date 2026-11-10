"""Os comandos do testador no turno (D56): trocar de caso, recomeçar e o que mais vier.

O testador escreve `/caso`, `/caso <n>`, `/paguei`, `/paguei <n>`, `/nota <texto>` ou o
`comando_reinicio` da política no WhatsApp. O comando nunca chega ao LLM: `antes_do_turno` o trata
antes de qualquer gate, grava a mensagem e a resposta com a origem TESTE (fora do histórico do LLM e
do teto) e consome a rajada. Tudo o que o modo teste diz ao testador começa com `[TESTE]`.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy.orm import Session

from agente.db.historico import abrir_turno, fechar_turno, registrar_mensagem
from agente.db.modelos import Cobranca, Conversa, Mensagem, Turno
from agente.db.travas import travar_conversas_e_cobranca
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn_teste import CasoInexistente, numero_do_caso
from agente.politica import CasoTeste, Politica
from agente.teste.casos import ativar_caso, do_piloto
from agente.tipos import OrigemMensagem, ResultadoTurno, StatusCobranca
from agente.turno.fila import agendar_turno, mensagens_pendentes, tomar_turno
from agente.turno.gates import humano_na_conversa

if TYPE_CHECKING:
    from agente.turno.orquestrador import Dependencias

log = logging.getLogger(__name__)

NomeDoComando = Literal["caso", "paguei", "nota", "reset", "desconhecido"]

# O que o turno de comando grava como ação principal: `TESTE:<NOME>` (o último comando tratado).
ROTULO_ENCERRADO = "TESTE:ENCERRADO"
TEXTO_ENCERRADO = (
    "[TESTE] Este caso terminou. Mande /caso para ver os casos ou /caso <n> para começar outro."
)
_NUMERO = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class Comando:
    """Um comando lido do texto. `numero` é o argumento de `/caso <n>` e `/paguei <n>`; `texto` é o
    argumento de `/nota <texto>` e, no `desconhecido`, o texto inteiro que o testador mandou."""

    nome: NomeDoComando
    numero: int | None
    texto: str


def ler_comando(texto: str, politica: Politica) -> Comando | None:
    """O comando que o texto traz, ou None se é conversa comum.

    Sem diferença de caixa nem de espaço nas pontas. Todo texto que começa com `/` e não é um
    comando conhecido é `desconhecido` (a resposta é a lista de comandos). O reinício é o
    `comando_reinicio` da política (no `dev2`, `/reset`).
    """
    limpo = texto.strip()
    if not limpo:
        return None
    if limpo.casefold() == politica.comando_reinicio.casefold():
        return Comando("reset", None, "")
    if not limpo.startswith("/"):
        return None
    partes = limpo[1:].split(None, 1)
    verbo = partes[0].casefold() if partes else ""
    resto = partes[1].strip() if len(partes) > 1 else ""
    numero = int(resto) if _NUMERO.fullmatch(resto) else None
    if verbo == "caso":
        return Comando("caso", numero, "" if numero is not None else resto)
    if verbo == "paguei" and (not resto or numero is not None):
        return Comando("paguei", numero, "")
    if verbo == "nota":
        return Comando("nota", None, resto)
    return Comando("desconhecido", None, limpo)


def _lista_de_casos(politica: Politica) -> str:
    linhas = [
        f"{caso.numero}. {caso.titulo} (dívida {_divida(caso, politica)})"
        for caso in politica.teste.casos
    ]
    return "Casos:\n" + "\n".join(linhas) + "\nMande /caso <n> para escolher um."


def _divida(caso: CasoTeste, politica: Politica) -> str:
    return formatar_reais(caso.meses_atraso * politica.mensalidade_centavos)


def _lista_de_comandos(politica: Politica) -> str:
    return (
        "[TESTE] Comando não reconhecido. Comandos: /caso (lista os casos), /caso <n> (começa o "
        "caso n), /paguei (confirma o Pix ou o link pedido), /paguei <n> (paga n meses por fora), "
        f"/nota <texto> (anota uma observação) e {politica.comando_reinicio} (recomeça o caso "
        "atual)."
    )


@dataclass
class _Rajada:
    """O que os tratadores dos comandos precisam: a conversa onde o comando chegou, a sessão e as
    dependências do turno."""

    sessao: Session
    conversa: Conversa
    turno: Turno
    deps: "Dependencias"
    agora: datetime

    @property
    def politica(self) -> Politica:
        return self.deps.politica


def _responder(rajada: _Rajada, texto: str) -> None:
    """Envia ao testador e grava com a origem TESTE, fora do teto. Se o Chatwoot falhar, a falha
    sobe e o turno inteiro é desfeito."""
    conversa = rajada.conversa
    confirmacao = rajada.deps.chatwoot.enviar_mensagem(conversa.chatwoot_conversa_id, texto)
    registrar_mensagem(
        rajada.sessao, conversa.id, OrigemMensagem.TESTE, texto, confirmacao, False, rajada.agora
    )


def _caso_atual(conversa: Conversa, politica: Politica) -> int:
    """O número do caso da cobrança da conversa; sem ele (cobrança que não é simulada), o padrão."""
    return numero_do_caso(conversa.cobranca.cliente.matricula_ctn) or politica.teste.caso_padrao


def _tratar_caso(rajada: _Rajada, comando: Comando) -> Cobranca | None:
    """`/caso`, `/caso <n>` e o reinício. Devolve a cobrança nova se o caso trocou."""
    politica = rajada.politica
    numero = _caso_atual(rajada.conversa, politica) if comando.nome == "reset" else comando.numero
    if numero is None:
        _responder(rajada, "[TESTE] " + _lista_de_casos(politica))
        return None
    caso = next((c for c in politica.teste.casos if c.numero == numero), None)
    if caso is None:
        _responder(rajada, f"[TESTE] Não existe o caso {numero}. " + _lista_de_casos(politica))
        return None
    conversa = rajada.conversa
    # Na ordem de `db/travas.py`: um `Pix` pago na mesma hora não pode ser encerrado por baixo.
    travar_conversas_e_cobranca(rajada.sessao, conversa.cobranca_id)
    try:
        cobranca = ativar_caso(
            rajada.sessao,
            rajada.deps.casos,
            politica,
            conversa.cobranca.cliente.telefone,
            numero,
            rajada.agora,
        )
    except CasoInexistente:  # o cardápio já foi conferido acima; só por segurança
        _responder(rajada, f"[TESTE] Não existe o caso {numero}. " + _lista_de_casos(politica))
        return None
    _responder(
        rajada,
        f"[TESTE] Agora você é o caso {caso.numero}: {caso.titulo}. CPF fictício: {caso.cpf}. "
        "Mande qualquer mensagem para o agente começar.",
    )
    return cobranca


def _tratar(rajada: _Rajada, comando: Comando) -> Cobranca | None:
    if comando.nome in ("caso", "reset"):
        return _tratar_caso(rajada, comando)
    if comando.nome == "desconhecido":
        _responder(rajada, _lista_de_comandos(rajada.politica))
        return None
    # O `/paguei` e o `/nota` entram nas tarefas 2.2 e 2.3.
    _responder(rajada, f"[TESTE] O comando /{comando.nome} ainda não está disponível aqui.")
    return None


def _trocar_de_conversa(
    rajada: _Rajada, cobranca: Cobranca, mensagens: list[Mensagem], posicao: int
) -> Conversa:
    """A conversa do Chatwoot passa para uma conversa nova, da cobrança nova; a antiga fica no
    histórico (como o `_reiniciar` do orquestrador).

    As mensagens da rajada que vieram depois do comando passam para a nova, com um turno agendado:
    sem isso, `/caso 2` e `Oi` dentro dos mesmos 7 s deixavam o `Oi` sem resposta. Com humano na
    conversa, a do Chatwoot volta a Pendente (o robô assume).
    """
    sessao, antiga, agora = rajada.sessao, rajada.conversa, rajada.agora
    chatwoot_id = antiga.chatwoot_conversa_id
    com_humano = humano_na_conversa(antiga)
    antiga.chatwoot_conversa_id = None
    sessao.flush()
    nova = Conversa(cobranca_id=cobranca.id, chatwoot_conversa_id=chatwoot_id)
    sessao.add(nova)
    sessao.flush()
    depois = mensagens[posicao + 1 :]
    for mensagem in depois:
        mensagem.conversa_id = nova.id
    # O turno do comando fica só com o que ele consumiu; o resto é do turno da conversa nova.
    rajada.turno.mensagens_ids = [m.id for m in mensagens[: posicao + 1]]
    if depois:
        agendar_turno(sessao, nova.id, agora)
    if com_humano:
        rajada.deps.chatwoot.mudar_status(chatwoot_id, "pending")
    sessao.flush()
    return nova


def _fechar_e_confirmar(
    sessao: Session, conversa: Conversa, turno: Turno, rotulo: str, agora: datetime
) -> ResultadoTurno:
    fechar_turno(sessao, turno.id, ResultadoTurno.FIXA, acao_principal=rotulo, momento=agora)
    conversa.falhas_turno = 0
    conversa.erros_turno = 0
    sessao.commit()
    return ResultadoTurno.FIXA


def _tratar_comandos(
    sessao: Session, conversa: Conversa, agora: datetime, deps: "Dependencias"
) -> ResultadoTurno:
    mensagens = tomar_turno(sessao, conversa.id)
    turno = abrir_turno(sessao, conversa.id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    rajada = _Rajada(sessao, conversa, turno, deps, agora)
    rotulo = ""
    for posicao, mensagem in enumerate(mensagens):
        comando = ler_comando(mensagem.texto, deps.politica)
        if comando is None:
            # Conversa comum no meio da rajada: o comando vence e ela é consumida sem resposta (P9).
            continue
        mensagem.origem = OrigemMensagem.TESTE
        rotulo = f"TESTE:{comando.nome.upper()}"
        cobranca = _tratar(rajada, comando)
        if cobranca is not None:
            _trocar_de_conversa(rajada, cobranca, mensagens, posicao)
            # O resto da rajada é da conversa nova (e dos comandos que ela ainda traga).
            break
    return _fechar_e_confirmar(sessao, conversa, turno, rotulo, agora)


def _tratar_encerrado(
    sessao: Session, conversa: Conversa, agora: datetime, deps: "Dependencias"
) -> ResultadoTurno:
    """A mensagem comum de um caso que terminou (paga ou trocada): só a orientação, sem o LLM."""
    mensagens = tomar_turno(sessao, conversa.id)
    turno = abrir_turno(sessao, conversa.id, [m.id for m in mensagens], deps.modo)
    turno.humano_na_conversa = humano_na_conversa(conversa)
    _responder(_Rajada(sessao, conversa, turno, deps, agora), TEXTO_ENCERRADO)
    return _fechar_e_confirmar(sessao, conversa, turno, ROTULO_ENCERRADO, agora)


def antes_do_turno(
    sessao: Session, conversa: Conversa, agora: datetime, deps: "Dependencias"
) -> ResultadoTurno | None:
    """O que o modo teste faz antes do turno normal. `None` = o turno segue.

    Só com `deps.casos` e a conversa em modo ativo (quem chama confere). Os comandos e o "caso
    encerrado" vêm antes da checagem de cobrança ATIVA e da de humano na conversa: o testador
    troca de caso mesmo depois de um handoff. Só vale para o telefone do piloto e para a cobrança
    da campanha de teste; a carteira real continua no turno de sempre.
    """
    teste = deps.politica.teste
    if deps.casos is None or teste is None:
        return None
    cobranca = conversa.cobranca
    if cobranca.campanha != teste.campanha or not do_piloto(
        cobranca.cliente.telefone, deps.politica
    ):
        return None
    pendentes = mensagens_pendentes(sessao, conversa.id)
    if not pendentes:
        return None
    if any(ler_comando(m.texto, deps.politica) is not None for m in pendentes):
        return _tratar_comandos(sessao, conversa, agora, deps)
    if cobranca.status is not StatusCobranca.ATIVA:
        return _tratar_encerrado(sessao, conversa, agora, deps)
    return None
