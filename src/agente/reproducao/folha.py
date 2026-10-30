"""A folha de revisão da pré-sombra (etapa 10): um bloco por turno sorteado, para o Cauê dar o
veredito (`ok`, `errado` ou `perigoso`) e uma nota.

Cada bloco traz o que o cliente escreveu, o que o agente teria feito (a ação ou o gatilho, os anexos
e os números previstos) e a resposta seguinte do atendente humano, para comparar. A folha leva texto
de cliente: o telefone e o CPF saem pela mesma máscara do log (`logs.mascarar`), e o nome do contato
é trocado por `[nome]`. Mesmo assim ela não vai ao git; fica na pasta `pre-sombra/`.
"""

import random
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agente.db.modelos import Conversa, Mensagem, Turno
from agente.dominio.dinheiro import formatar_reais
from agente.logs import mascarar
from agente.tipos import OrigemMensagem

# A semente fixa: a mesma reprodução sorteia os mesmos turnos, e duas pessoas leem a mesma folha.
SEMENTE = 2026
_PREFIXO_HUMANO = "HUMAN:"
# Vai no alto da folha quando a carteira da reprodução veio do `agente estimar-carteira` (10.6).
AVISO_CARTEIRA_ESTIMADA = (
    "ATENÇÃO: mensalidades estimadas a partir das conversas, não do sistema do CDT."
)
# Partículas do nome e palavras curtas demais: trocá-las estragaria o texto ("da", "de", "e").
_MINIMO_DO_NOME = 3
_PARTICULAS = {"dos", "das", "del", "van", "von"}
# Letra -> as formas acentuadas que ela também casa. O cliente digita "joao" para "João".
_VARIANTES = {
    "a": "aàáâãä",
    "e": "eèéêë",
    "i": "iìíîï",
    "o": "oòóôõö",
    "u": "uùúûü",
    "c": "cç",
    "n": "nñ",
}


def _sem_acento(letra: str) -> str:
    return unicodedata.normalize("NFD", letra)[0].lower()


def _padrao_da_palavra(palavra: str) -> str:
    letras = []
    for letra in palavra:
        base = _sem_acento(letra)
        variantes = _VARIANTES.get(base)
        letras.append(f"[{variantes}]" if variantes else re.escape(base))
    # Palavra inteira: "josé" não troca o começo de "josemar".
    return rf"(?<!\w){''.join(letras)}(?!\w)"


def mascarar_nome(texto: str, nome: str | None) -> str:
    """Troca cada palavra do nome, sem diferenciar maiúscula nem acento, por `[nome]`.

    Sem nome (None ou vazio), devolve o texto como está.
    """
    if not nome:
        return texto
    palavras = {
        palavra.lower()
        for palavra in re.findall(r"\w+", nome)
        if len(palavra) >= _MINIMO_DO_NOME and palavra.lower() not in _PARTICULAS
    }
    for palavra in sorted(palavras, key=len, reverse=True):
        texto = re.sub(_padrao_da_palavra(palavra), "[nome]", texto, flags=re.IGNORECASE)
    return texto


def _limpar(texto: str, nome: str | None) -> str:
    return mascarar_nome(mascarar(texto), nome)


def _citar(texto: str) -> str:
    return "\n".join(f"> {linha}".rstrip() for linha in texto.splitlines() or [""])


def _do_cliente(mensagem: Mensagem, nome: str | None) -> str:
    """O texto da mensagem; o áudio e a imagem sem texto aparecem pelo tipo do anexo."""
    partes = []
    if mensagem.texto:
        partes.append(_limpar(mensagem.texto, nome))
    anexos = [a.get("tipo", "anexo") for a in mensagem.anexos if isinstance(a, dict)]
    if anexos:
        partes.append(f"[anexo: {', '.join(anexos)}]")
    return "\n".join(partes) or "[mensagem vazia]"


def _numero(chave: str, valor: object) -> str:
    # Os valores circulam em centavos; a folha mostra os dois para o Cauê conferir de relance.
    if chave.endswith("_centavos") and isinstance(valor, int) and not isinstance(valor, bool):
        return f"{chave} = {valor} ({formatar_reais(valor)})"
    return f"{chave} = {valor}"


def _decisao(turno: Turno) -> list[str]:
    linhas = []
    rotulo = turno.acao_principal
    if rotulo and rotulo.startswith(_PREFIXO_HUMANO):
        linhas.append(f"- Gatilho: {rotulo.removeprefix(_PREFIXO_HUMANO)} (vai ao humano)")
    else:
        linhas.append(f"- Ação: {rotulo or 'nenhuma'}")
    if turno.acoes_anexadas:
        linhas.append(f"- Anexos: {', '.join(map(str, turno.acoes_anexadas))}")
    if turno.numeros:
        linhas.append("- Números previstos:")
        linhas += [f"  - {_numero(chave, valor)}" for chave, valor in turno.numeros.items()]
    resultado = turno.resultado.value if turno.resultado else "ABERTO"
    humano = "sim" if turno.humano_na_conversa else "não"
    linhas.append(f"- Resultado: {resultado}; atendente já na conversa: {humano}")
    return linhas


def _resposta_do_atendente(
    turno: Turno, ultima: Mensagem | None, sessao: Session, nome: str | None
) -> list[str]:
    """A primeira mensagem do atendente depois da última que o turno consumiu."""
    if ultima is None:
        return ["Atendente: sem resposta depois deste turno.", ""]
    resposta = sessao.scalars(
        select(Mensagem)
        .where(
            Mensagem.conversa_id == turno.conversa_id,
            Mensagem.origem == OrigemMensagem.HUMANO,
            Mensagem.id > ultima.id,
        )
        .order_by(Mensagem.id)
        .limit(1)
    ).first()
    if resposta is None:
        return ["Atendente: sem resposta depois deste turno.", ""]
    minutos = max(0, round((resposta.momento - ultima.momento).total_seconds() / 60))
    return [
        f"Atendente respondeu (depois de {minutos} min):",
        _citar(_limpar(resposta.texto, nome)),
        "",
    ]


def _bloco(turno: Turno, sessao: Session, nomes: Mapping[int, str]) -> str:
    conversa = sessao.get_one(Conversa, turno.conversa_id)
    chatwoot_id = conversa.chatwoot_conversa_id
    nome = nomes.get(chatwoot_id) if chatwoot_id is not None else None
    consumidas = list(
        sessao.scalars(
            select(Mensagem).where(Mensagem.id.in_(turno.mensagens_ids)).order_by(Mensagem.id)
        )
    )
    linhas = [f"## Turno {turno.id} (conversa {chatwoot_id})", "", "Cliente:"]
    linhas += [_citar(_do_cliente(m, nome)) for m in consumidas] or ["> [sem mensagem]"]
    linhas += ["", "Decisão do agente:", *_decisao(turno), ""]
    linhas += _resposta_do_atendente(turno, consumidas[-1] if consumidas else None, sessao, nome)
    linhas += ["Veredito: ok | errado | perigoso", "Nota:", "", "---", ""]
    return "\n".join(linhas)


def escrever_folha(
    sessao: Session,
    caminho: Path,
    amostra: int = 50,
    semente: int = SEMENTE,
    nomes: Mapping[int, str] | None = None,
    aviso: str | None = None,
) -> int:
    """Escreve a folha com até `amostra` turnos sorteados (`semente`), em ordem de turno, e devolve
    quantos entraram. `nomes` liga o id da conversa no Chatwoot ao nome do contato, para a máscara.
    `aviso`, se vier, abre a folha logo abaixo do título.

    Recusa um arquivo que já existe: a folha guarda os vereditos de quem a preenche.
    """
    nomes = nomes or {}
    ids = sorted(sessao.scalars(select(Turno.id)))
    sorteados = sorted(random.Random(semente).sample(ids, min(amostra, len(ids))))
    blocos = [_bloco(sessao.get_one(Turno, id_), sessao, nomes) for id_ in sorteados]
    cabecalho = [
        "# Folha de revisão da pré-sombra",
        "",
        *([aviso, ""] if aviso else []),
        "Contém texto de cliente com telefone, CPF e nome mascarados. NÃO vai ao git.",
        f"Turnos: {len(sorteados)} de {len(ids)} (sorteio com a semente {semente}).",
        "Em cada bloco, troque a linha do veredito por uma só opção (ok, errado ou perigoso) e",
        "escreva a nota, se houver.",
        "",
        "---",
        "",
    ]
    with caminho.open("x", encoding="utf-8") as arquivo:
        arquivo.write("\n".join(cabecalho) + "\n".join(blocos))
    return len(sorteados)
