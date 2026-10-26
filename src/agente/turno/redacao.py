"""Marcadores e interpolação: o LLM escreve `{valor_final}`, e o backend preenche (seção 5).

Um número errado não tem por onde entrar: o texto do LLM com dígito, com R$, com valor ou prazo por
extenso, com link próprio, com marcador desconhecido, sem valor no turno ou faltando o de uma ação é
recusado com `RedacaoInvalida`.
"""

import re
import unicodedata
from dataclasses import dataclass, field, replace

from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.jev import EstadoJev, Jev, JevIndisponivel
from agente.integracoes.redator import PedidoRedacao, Redator, RedatorIndisponivel
from agente.politica import Politica, Regua
from agente.tipos import EstadoConversa, Laudo, MotivoHumano
from agente.turno.acoes import Acao
from agente.turno.executar import Numeros
from agente.turno.jev1 import LeituraJev1
from agente.turno.jev3 import validar

_MARCADOR = re.compile(r"\{([a-z_]+)\}")
# Reprovou: o LLM reescreve uma vez, sabendo o que reprovou (seção 6).
TENTATIVAS = 2
_DIGITO = re.compile(r"\d")

# Valor e prazo por extenso (sem acento, em minúsculas). O numeral sozinho é palavra comum ("os
# dois", "zero burocracia", "mil desculpas", "o problema real"): só é barrado perto de uma palavra
# de dinheiro, de prazo ou de "por cento", ou quando faz um número composto ("duzentos e trinta").
_NUMERAIS = (
    "zero|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|catorze|quatorze"
    "|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta"
    "|setenta|oitenta|noventa|cem|cento|duzentos|duzentas|trezentos|trezentas|quatrocentos"
    "|quatrocentas|quinhentos|quinhentas|seiscentos|seiscentas|setecentos|setecentas"
    "|oitocentos|oitocentas|novecentos|novecentas"
)
_DINHEIRO_E_PRAZO = "reais|real|dias?|mes|meses|semanas?|anos?|horas?|parcelas?|mensalidades?"
_POR_EXTENSO = re.compile(
    r"%|\b(?:"
    # "dez por cento", "cinquenta centavos", "um milhão" sempre falam de valor.
    r"por cento|centavos?|milhao|milhoes"
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
# Todo link vem do backend, em `{link_ctn}`: sobra "http", "www" e domínio solto.
_LINK = re.compile(
    r"\bhttps?\b|\bwww\b|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*(?P<tld>\.(?:com|net|org|br|io|app|me|co|ly"
    r"|link|info|biz|gov|edu|xyz|site|online|dev|invalid|example))\b",
    re.IGNORECASE,
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
    "pix_vencimento",
)


def valores(numeros: Numeros) -> dict[str, str]:
    """Os marcadores que o turno tem como preencher, já formatados em pt-BR."""
    preenchidos = {
        "divida": formatar_reais(numeros.divida_centavos),
        "mensalidades": str(numeros.mensalidades),
    }
    if numeros.link_ctn is not None:
        # O desconto só vale no link (D34): sem link, não há valor com desconto para citar.
        preenchidos["valor_final"] = formatar_reais(numeros.valor_final_centavos)
        if numeros.degrau > 0:
            preenchidos["desconto"] = formatar_reais(numeros.desconto_centavos)
        preenchidos["link_ctn"] = numeros.link_ctn
    if numeros.br_code is not None:
        preenchidos["valor_pix"] = formatar_reais(numeros.valor_pix_centavos)
        preenchidos["pix_copia_e_cola"] = numeros.br_code
        preenchidos["pix_vencimento"] = numeros.pix_vence_em.strftime("%d/%m às %H:%M")
    return preenchidos


# O marcador que o texto tem de trazer para cobrir cada ação do turno (Jev 3: bate_com_acao).
OBRIGATORIOS = {
    Acao.INFORM_DEBT: "divida",
    Acao.OFFER_DISCOUNT: "valor_final",
    Acao.SEND_CTN_LINK: "link_ctn",
    Acao.GENERATE_PIX: "pix_copia_e_cola",
}

# O tom da resposta a partir do tom do cliente (Jev 1). Irritado e hostil já saíram para o humano.
TOM_ALVO = {
    "cordial": "cordial",
    "neutro": "cordial e direto",
    "impaciente": "breve e direto, reconhecendo o incômodo",
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


def interpolar(texto: str, numeros: Numeros) -> str:
    """Troca os marcadores pelos números do backend, formatados em pt-BR."""
    motivos = []
    fora = _MARCADOR.sub("", texto)
    if _DIGITO.search(fora) or "R$" in fora:
        motivos.append("valor_em_digitos")
    simples = _sem_acento(fora).lower()
    if _POR_EXTENSO.search(simples):
        motivos.append("valor_por_extenso")
    if _tem_link(_sem_acento(fora)):
        motivos.append("link_nao_autorizado")
    if "{" in fora or "}" in fora:
        motivos.append("marcador_malformado")
    usados = _MARCADOR.findall(texto)
    preenchidos = valores(numeros)
    for nome in dict.fromkeys(usados):
        if nome not in MARCADORES:
            motivos.append(f"marcador_desconhecido:{nome}")
        elif nome not in preenchidos:
            motivos.append(f"marcador_sem_valor:{nome}")
    obrigatorios, _ = marcadores_do_turno(numeros)
    motivos += [f"marcador_faltando:{nome}" for nome in obrigatorios if nome not in usados]
    if motivos:
        raise RedacaoInvalida(motivos)
    return _MARCADOR.sub(lambda achado: preenchidos[achado.group(1)], texto)


def montar_pedido(
    numeros: Numeros,
    leitura: LeituraJev1 | None,
    regua: Regua,
    nome_cliente: str,
    estado: EstadoConversa,
    historico: list[tuple[str, str]],
    politica: Politica,
    reprovadas_antes: list[str] | None = None,
) -> PedidoRedacao:
    obrigatorios, opcionais = marcadores_do_turno(numeros)
    sem_desconto = politica.reguas[regua].desconto_max_centavos == 0
    return PedidoRedacao(
        acoes=tuple(acao.value for acao in numeros.acoes),
        marcadores=tuple(obrigatorios),
        # Sem leitura (o toque no botão não passa pelo Jev 1), o tom é o neutro.
        tom_alvo=TOM_ALVO.get(leitura.tom if leitura else "", TOM_ALVO["neutro"]),
        estado=estado.value,
        historico=tuple(historico),
        nome_cliente=nome_cliente.split()[0],
        reprovadas_antes=tuple(reprovadas_antes) if reprovadas_antes else None,
        marcadores_opcionais=tuple(opcionais),
        objecao_sem_desconto=leitura is not None and leitura.sim("objecao_valor") and sem_desconto,
    )


@dataclass(frozen=True)
class Tentativa:
    """Um texto do LLM, com os marcadores, e o que reprovou. Sem reprovadas, passou."""

    texto: str
    reprovadas: list[str]
    # Os laudos do Jev 3; vazios quando a interpolação recusou o texto antes dele.
    laudos: tuple[Laudo, ...] = ()


@dataclass
class Redacao:
    """O resultado: o texto final aprovado ou, sem ele, o motivo de ir para o humano."""

    tentativas: list[Tentativa] = field(default_factory=list)
    texto: str | None = None
    # Com o Jev ou o LLM fora, o turno vai para o humano (seção 9).
    motivo: MotivoHumano | None = None
    erro: str | None = None

    def como_dict(self, saida: str) -> dict:
        """Para o `turno.redacao`: as tentativas, o erro e o que saiu."""
        dados: dict = {
            "tentativas": [{"texto": t.texto, "reprovadas": t.reprovadas} for t in self.tentativas],
            "saida": saida,
        }
        if self.erro is not None:
            dados["erro"] = self.erro
        return dados


def redigir_validado(
    redator: Redator, jev: Jev, pedido: PedidoRedacao, numeros: Numeros, estado: EstadoJev
) -> Redacao:
    """Redige, interpola e valida no Jev 3, com uma reescrita. Não grava nem envia nada."""
    redacao = Redacao()
    reprovadas: list[str] = []
    for _ in range(TENTATIVAS):
        try:
            texto = redator.redigir(replace(pedido, reprovadas_antes=tuple(reprovadas) or None))
        except RedatorIndisponivel as erro:
            redacao.motivo, redacao.erro = MotivoHumano.REDATOR_INDISPONIVEL, str(erro)
            return redacao
        try:
            final = interpolar(texto, numeros)
        except RedacaoInvalida as erro:
            # O texto que não interpola nem chega ao Jev 3.
            reprovadas = erro.motivos
            redacao.tentativas.append(Tentativa(texto, reprovadas))
            continue
        try:
            validacao = validar(jev, final, list(numeros.acoes), numeros.como_dict(), estado)
        except JevIndisponivel as erro:
            redacao.motivo, redacao.erro = MotivoHumano.JEV_INDISPONIVEL, str(erro)
            return redacao
        redacao.tentativas.append(Tentativa(texto, validacao.reprovadas, validacao.laudos))
        if validacao.aprovado:
            redacao.texto = final
            return redacao
        reprovadas = validacao.reprovadas
    return redacao
