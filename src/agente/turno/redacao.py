"""Marcadores e interpolação: o LLM escreve `{valor_final}`, e o backend preenche (seção 5).

Um número errado não tem por onde entrar: o texto do LLM com dígito, com R$, com marcador
desconhecido, sem valor no turno ou faltando o de uma ação é recusado com `RedacaoInvalida`.
"""

import re

from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.redator import PedidoRedacao
from agente.politica import Politica, Regua
from agente.tipos import EstadoConversa
from agente.turno.acoes import Acao
from agente.turno.executar import Numeros
from agente.turno.jev1 import LeituraJev1

_MARCADOR = re.compile(r"\{([a-z_]+)\}")
_DIGITO = re.compile(r"\d")


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


def interpolar(texto: str, numeros: Numeros) -> str:
    """Troca os marcadores pelos números do backend, formatados em pt-BR."""
    motivos = []
    fora = _MARCADOR.sub("", texto)
    if _DIGITO.search(fora) or "R$" in fora:
        motivos.append("valor_em_digitos")
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
    leitura: LeituraJev1,
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
        tom_alvo=TOM_ALVO.get(leitura.tom, TOM_ALVO["neutro"]),
        estado=estado.value,
        historico=tuple(historico),
        nome_cliente=nome_cliente.split()[0],
        reprovadas_antes=tuple(reprovadas_antes) if reprovadas_antes else None,
        marcadores_opcionais=tuple(opcionais),
        objecao_sem_desconto=leitura.sim("objecao_valor") and sem_desconto,
    )
