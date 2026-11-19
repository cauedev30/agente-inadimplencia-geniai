"""Marcadores e interpolação: o LLM escreve `{valor_final}`, e o backend preenche (seção 5).

Um número errado não tem por onde entrar: o texto do LLM com dígito, com R$, com valor ou prazo por
extenso, com link próprio, com marcador desconhecido, sem valor no turno ou faltando o de uma ação é
recusado com `RedacaoInvalida`.
"""

import re
import unicodedata

from agente.dominio.dinheiro import formatar_reais
from agente.politica import Politica
from agente.turno.acoes import Acao
from agente.turno.executar import Numeros

_MARCADOR = re.compile(r"\{([a-z_]+)\}")

# Valor e prazo por extenso (sem acento, em minúsculas). Os numerais pequenos são palavra comum
# ("os dois", "zero burocracia", "mil desculpas", "o problema real", "um abraço"): "zero", "um/uma",
# "dois/duas", "tres" a "nove" e "mil" só são barrados perto de uma palavra de dinheiro, de prazo ou
# de "por cento", ou quando fazem um número composto ("duzentos e trinta"). De "dez" a
# "novecentos", "cem", "cento" e "metade" quase só aparecem falando de valor ("sai por noventa"):
# são barrados até soltos.
_NUMERAIS = (
    "zero|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|catorze|quatorze"
    "|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta"
    "|setenta|oitenta|noventa|cem|cento|duzentos|duzentas|trezentos|trezentas|quatrocentos"
    "|quatrocentas|quinhentos|quinhentas|seiscentos|seiscentas|setecentos|setecentas"
    "|oitocentos|oitocentas|novecentos|novecentas"
)
_NUMERAIS_SOLTOS = (
    "dez|onze|doze|treze|catorze|quatorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte"
    "|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzentos|duzentas"
    "|trezentos|trezentas|quatrocentos|quatrocentas|quinhentos|quinhentas|seiscentos|seiscentas"
    "|setecentos|setecentas|oitocentos|oitocentas|novecentos|novecentas|metade"
)
_DINHEIRO_E_PRAZO = "reais|real|dias?|mes|meses|semanas?|anos?|horas?|parcelas?|mensalidades?"
_POR_EXTENSO = re.compile(
    r"%|\b(?:"
    # "sai por noventa", "fica por cem", "pague a metade".
    rf"{_NUMERAIS_SOLTOS}"
    # "dez por cento", "cinquenta centavos", "um milhão" sempre falam de valor.
    r"|por cento|centavos?|milhao|milhoes"
    # "dois mil", "um mil", "mil e quinhentos", "mil reais".
    rf"|(?:{_NUMERAIS}|um|uma)\s+mil|mil\s+e\s+(?:{_NUMERAIS})|mil\s+(?:{_DINHEIRO_E_PRAZO})"
    # "duzentos e cinquenta".
    rf"|(?:{_NUMERAIS})\s+e\s+(?:{_NUMERAIS})"
    # "setenta reais", "em trinta dias", "as duas parcelas": até duas palavras entre os dois.
    rf"|(?:{_NUMERAIS})(?:\s+[a-z]+){{0,2}}?\s+(?:{_DINHEIRO_E_PRAZO})"
    # "um/uma" é artigo ("um abraço"): só conta colado a uma palavra de dinheiro ou de prazo.
    rf"|(?:um|uma)\s+(?:{_DINHEIRO_E_PRAZO})"
    r")\b"
)
# Todo link vem do backend, em `{link_ctn}`: sobra "http", "www" e domínio solto. Qualquer
# `palavra.tld` com o tld em minúsculas (de 2 a 24 letras) é domínio, seja qual for o tld; com
# maiúscula, só os tlds comuns, porque "Obrigado.Até logo" é o ponto colado entre duas frases.
_LINK = re.compile(
    r"(?i:\bhttps?\b|\bwww\b)"
    r"|\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?P<tld>\.(?:[a-z]{2,24}"
    r"|(?i:com|net|org|br|io|app|me|co|ly|link|info|biz|gov|edu|xyz|site|online|dev|invalid"
    r"|example)))\b"
)
# "agora.Com o valor": o ponto final sem espaço, com a próxima frase começando em maiúscula. Só
# "Com" e "Me" abrem frase em português; os outros TLDs ("Online", "Site", "Link") ficam barrados.
_FRASE_COLADA = re.compile(r"\.(?:Com|Me)\s+[a-z]")


# Todo marcador que o LLM pode usar. O valor de cada um vem do backend, em `valores`.
MARCADORES = (
    "divida",
    "mensalidades",
    "valor_final",
    "desconto",
    "link_ctn",
    "valor_pix",
    "pix_copia_e_cola",
)


def valores(numeros: Numeros) -> dict[str, str]:
    """Os marcadores que o turno tem como preencher, já formatados em pt-BR."""
    preenchidos = {
        "divida": formatar_reais(numeros.divida_centavos),
        "mensalidades": str(numeros.mensalidades),
    }
    # O valor com desconto vale em qualquer meio (D64), mas só existe depois da oferta (degrau > 0).
    if numeros.degrau > 0:
        preenchidos["valor_final"] = formatar_reais(numeros.valor_final_centavos)
        preenchidos["desconto"] = formatar_reais(numeros.desconto_centavos)
    if numeros.link_ctn is not None:
        preenchidos["link_ctn"] = numeros.link_ctn
    if numeros.br_code is not None:
        preenchidos["valor_pix"] = formatar_reais(numeros.valor_pix_centavos)
        preenchidos["pix_copia_e_cola"] = numeros.br_code
    return preenchidos


# O marcador que o texto tem de trazer para cobrir cada ação do turno (a prévia do turno confere).
OBRIGATORIOS = {
    Acao.INFORM_DEBT: "divida",
    Acao.OFFER_DISCOUNT: "valor_final",
    Acao.SEND_CTN_LINK: "link_ctn",
    Acao.GENERATE_PIX: "pix_copia_e_cola",
}


class RedacaoInvalida(ValueError):
    """O texto do LLM não pode ser interpolado; os `motivos` vão para a reescrita."""

    def __init__(self, motivos: list[str]):
        super().__init__("; ".join(motivos))
        self.motivos = motivos


def marcadores_do_turno(numeros: Numeros) -> tuple[list[str], list[str]]:
    """Os obrigatórios, na ordem das ações, e os opcionais que o turno tem como preencher."""
    obrigatorios = [OBRIGATORIOS[acao] for acao in numeros.acoes if acao in OBRIGATORIOS]
    opcionais = [nome for nome in valores(numeros) if nome not in obrigatorios]
    return obrigatorios, opcionais


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _tem_numero(texto: str) -> bool:
    """Dígito de qualquer alfabeto, ou o caractere que vale um número ("①", "²", "½")."""
    return any(unicodedata.numeric(letra, None) is not None for letra in texto)


def _tem_link(texto: str) -> bool:
    """Link ou domínio no texto (sem acento, com a caixa original).

    "agora.Com o valor" é o ponto sem espaço entre duas frases, não um domínio: "Com" ou "Me" com a
    inicial maiúscula, depois de letra minúscula e antes de uma palavra minúscula.
    """
    for achado in _LINK.finditer(texto):
        inicio = achado.start("tld") if achado.group("tld") else None
        if inicio and _FRASE_COLADA.match(texto, inicio) and texto[inicio - 1].islower():
            continue
        return True
    return False


def motivos_do_texto(texto: str) -> list[str]:
    """Os motivos de recusa do texto fora dos marcadores, sem olhar os marcadores em si.

    Valor em dígitos, valor por extenso, link e marcador malformado: as checagens que o
    `interpolar` faz e que o texto do handoff do turno de uma chamada também precisa passar (D47).
    """
    motivos = []
    fora = _MARCADOR.sub("", texto)
    if _tem_numero(fora) or "R$" in fora:
        motivos.append("valor_em_digitos")
    simples = _sem_acento(fora).lower()
    if _POR_EXTENSO.search(simples):
        motivos.append("valor_por_extenso")
    if _tem_link(_sem_acento(fora)):
        motivos.append("link_nao_autorizado")
    if "{" in fora or "}" in fora:
        motivos.append("marcador_malformado")
    return motivos


_PIX = "{pix_copia_e_cola}"
# O espaço ou a quebra antes da chave e o ponto logo depois dela, quando nada mais segue na linha.
_EM_VOLTA_DA_CHAVE = re.compile(r"\s*\{pix_copia_e_cola\}[ \t]*[.!]?[ \t]*(?=\n|$)")
# O fim da frase anterior à do Pix, na mesma linha.
_ANTES_DA_FRASE_DO_PIX = re.compile(r"([.!?])[ \t]+(?=[^.!?\n]*\{pix_copia_e_cola\})")


def _arrumar_a_linha_do_pix(texto: str) -> str:
    """A frase do Pix numa linha nova, a chave colada ao "CNPJ:" e sem o ponto depois dela (o Cauê,
    2026-11-17). O Luna acerta as palavras e erra a quebra; o que não dá para arrumar (sem
    "CNPJ", texto depois da chave) a conferência reprova."""
    texto = _EM_VOLTA_DA_CHAVE.sub(" " + _PIX, texto)
    return _ANTES_DA_FRASE_DO_PIX.sub("\\1\n", texto)


def _pix_fora_da_linha(texto: str) -> bool:
    """A chave Pix nunca vai seca nem no meio da frase: numa linha só dela, depois da 1ª, com
    "CNPJ" antes e nada depois (o cliente copia a chave sem levar o ponto)."""
    for i, linha in enumerate(texto.split("\n")):
        antes, achou, depois = linha.partition(_PIX)
        if achou and (i == 0 or "cnpj" not in antes.lower() or depois.strip()):
            return True
    return False


# A linha da chave quando é o código que a escreve: a mesma que o glossário dá ao Luna.
LINHA_DO_PIX = "Para pagar, use o Pix CNPJ: " + _PIX


def _pix_pelo_codigo(texto: str) -> str:
    """O turno leva o Pix e o Luna esqueceu a chave ou a pôs fora do formato: o código tira as
    linhas com ela e escreve a linha da chave no fim (ensaio de 2026-11-17). O Luna redige o resto
    livre; só a linha da chave é do código. A frase que anunciava a chave ("use esta chave:") sai
    junto. Sem texto além da chave, nada muda e a conferência reprova."""
    corpo = "\n".join(linha for linha in texto.split("\n") if _PIX not in linha).rstrip()
    if corpo.endswith(":"):
        fim = max(corpo.rfind(sinal) for sinal in ".!?")
        corpo = corpo[: fim + 1] if fim >= 0 else corpo
    return f"{corpo}\n{LINHA_DO_PIX}" if corpo else texto


def interpolar(texto: str, numeros: Numeros) -> str:
    """Troca os marcadores pelos números do backend, formatados em pt-BR."""
    texto = _arrumar_a_linha_do_pix(texto)
    obrigatorios, _ = marcadores_do_turno(numeros)
    if "pix_copia_e_cola" in obrigatorios and (_PIX not in texto or _pix_fora_da_linha(texto)):
        texto = _pix_pelo_codigo(texto)
    motivos = motivos_do_texto(texto)
    if _pix_fora_da_linha(texto):
        motivos.append("pix_fora_da_linha_do_cnpj")
    usados = _MARCADOR.findall(texto)
    preenchidos = valores(numeros)
    for nome in dict.fromkeys(usados):
        if nome not in MARCADORES:
            motivos.append(f"marcador_desconhecido:{nome}")
        elif nome not in preenchidos:
            motivos.append(f"marcador_sem_valor:{nome}")
    motivos += [f"marcador_faltando:{nome}" for nome in obrigatorios if nome not in usados]
    if motivos:
        raise RedacaoInvalida(motivos)
    return _MARCADOR.sub(lambda achado: preenchidos[achado.group(1)], texto)


def anexar_recebedor(texto: str, numeros: Numeros, politica: Politica) -> str:
    """A frase do recebedor do Pix no fim do texto aprovado (D44), ou o texto igual.

    Só quando o turno traz o Pix e o `pix_recebedor` está preenchido. É o backend que a escreve, não
    o LLM, e ela entra depois da prévia: o nome do recebedor não passa pelo `interpolar`, que
    recusaria dígito (o CNPJ, por exemplo) e é feito para o texto do LLM.
    """
    if numeros.br_code is None or not politica.pix_recebedor:
        return texto
    frase = politica.texto_pix_recebedor.replace("{recebedor}", politica.pix_recebedor)
    # Em linha separada, para não ir junto quando o cliente copia o código Pix.
    return f"{texto}\n\n{frase}"
