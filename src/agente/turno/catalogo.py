"""Mensagens fixas: sem LLM e sem Jev, com os dados preenchidos pelo backend (D33, D19).

Os textos de confirmação são provisórios: o projeto desenha os finais (D28).
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from agente.dominio.dinheiro import formatar_reais
from agente.tipos import Botao, MotivoHumano, TipoFixa

# A 6ª mensagem e a saída padrão para o humano (D33, D60): sem botão e sem link, o atendente segue
# na mesma conversa.
TEXTO_CANAL_EXTERNO = (
    "Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui mesmo, "
    "nesta conversa."
)


# A tabela de políticas: o que o agente faz em cada situação fora do caminho do pagamento. Uma
# linha nova muda só a saída; detectar a situação continua sendo trabalho de um sinal, gate ou ação.
# A página `docs/politicas.md` é a versão que uma pessoa lê.


@dataclass(frozen=True)
class Frase:
    """A saída é esta frase pronta, sem o Luna; conta no teto como as outras mensagens."""

    texto: str


@dataclass(frozen=True)
class Atendente:
    """A saída é o atendente, com este motivo, e a frase fixa do handoff."""

    motivo: MotivoHumano


class Situacao(StrEnum):
    TEXTO_REPROVADO = "TEXTO_REPROVADO"
    SEM_RESPOSTA = "SEM_RESPOSTA"
    FOTO_SEM_TEXTO = "FOTO_SEM_TEXTO"
    FORA_DO_ASSUNTO = "FORA_DO_ASSUNTO"
    TERCEIRO = "TERCEIRO"


POLITICAS: dict[Situacao, Frase | Atendente] = {
    # Um texto que não passou duas vezes não vira outra tentativa de texto: o atendente segue.
    Situacao.TEXTO_REPROVADO: Atendente(MotivoHumano.VALIDACAO),
    # O Luna não tem o que mandar e a mensagem não é cortesia: o cliente não fica sem resposta.
    Situacao.SEM_RESPOSTA: Atendente(MotivoHumano.SEM_RESPOSTA),
    # O Luna não lê imagem; foto ou arquivo sem texto em geral é comprovante: alguém confere.
    Situacao.FOTO_SEM_TEXTO: Atendente(MotivoHumano.CONCILIACAO),
    # O agente só trata da mensalidade: o resto ganha uma frase pronta, sem o Luna afirmar nada.
    Situacao.FORA_DO_ASSUNTO: Frase(
        "Esse assunto não é tratado por aqui. Esta conversa é sobre a sua mensalidade em aberto."
    ),
    # Com quem não é o devedor não se fala da dívida: a frase pronta não deixa escapar nada.
    Situacao.TERCEIRO: Frase(
        "Obrigado pelo retorno. Este contato é para o titular do cadastro. Quando ele puder, é só "
        "falar com a gente por aqui."
    ),
}


# A mensagem que não pede resposta. Fechada e pequena: o que fica fora dela vai ao atendente, que é
# o erro seguro. "Oi" não está aqui, porque no meio da conversa ele pede a dívida de novo.
CORTESIA: frozenset[str] = frozenset(
    {
        "ok",
        "okay",
        "blz",
        "beleza",
        "ta bom",
        "certo",
        "entendi",
        "combinado",
        "obrigado",
        "obrigada",
        "obg",
        "valeu",
        "vlw",
    }
)


def _simplificar(texto: str) -> tuple[str, bool]:
    """O texto sem acento, pontuação, emoji e caixa, espaços normalizados; e se tinha emoji."""
    letras, tem_emoji = [], False
    for caractere in unicodedata.normalize("NFKD", texto):
        categoria = unicodedata.category(caractere)
        if categoria == "So":
            tem_emoji = True
        if categoria[0] == "M":
            # O acento, separado da letra pela decomposição, some sem partir a palavra.
            continue
        if categoria[0] in "PSCZ":
            # Pontuação (P), símbolo e emoji (S), controle e junção (C) e espaço (Z) separam.
            letras.append(" ")
        else:
            letras.append(caractere.lower())
    return " ".join("".join(letras).split()), tem_emoji


def e_cortesia(textos: Sequence[str]) -> bool:
    """Todas as mensagens novas estão na lista de cortesia ou são só emoji; sem mensagem, não."""
    if not textos:
        return False
    for texto in textos:
        simples, tem_emoji = _simplificar(texto)
        if simples not in CORTESIA and not (simples == "" and tem_emoji):
            return False
    return True


# A resposta ao toque em "Pagar com Pix" (D26, D59): fixa, com os marcadores que o `interpolar`
# preenche com o Pix do backend. Conta no teto como qualquer mensagem (D40). A frase do recebedor
# entra depois (`anexar_recebedor`), e o código vai sozinho no fim, para o cliente copiar.
TEXTO_DO_PIX = (
    "Aqui está o seu Pix de {valor_pix}, válido até {pix_vencimento}. "
    "Copie o código abaixo e cole no app do seu banco para pagar:\n\n{pix_copia_e_cola}"
)


@dataclass(frozen=True)
class DadosFixa:
    nome: str
    valor_centavos: int | None
    link_cartao: str | None
    # O texto que a política dá à fixa que não tem texto próprio (PEDIR_TEXTO e HUMANO_NO_WHATSAPP).
    texto: str | None = None


@dataclass(frozen=True)
class MensagemPronta:
    texto: str
    botoes: list[Botao] = field(default_factory=list)


def _confirmacao(dados: DadosFixa) -> str:
    if dados.valor_centavos is None:
        raise ValueError("a confirmação precisa do valor pago")
    valor = formatar_reais(dados.valor_centavos)
    return (
        f"Obrigado, {dados.nome}! Recebemos o seu pagamento de {valor}. "
        "A equipe do Cartão de Todos agradece."
    )


def mensagem_fixa(tipo: TipoFixa, dados: DadosFixa) -> MensagemPronta:
    if tipo is TipoFixa.CANAL_EXTERNO:
        return MensagemPronta(TEXTO_CANAL_EXTERNO)
    if tipo is TipoFixa.PEDIR_TEXTO:
        if not dados.texto:
            raise ValueError("o pedido de texto precisa do texto da política")
        return MensagemPronta(dados.texto)
    if tipo is TipoFixa.HUMANO_NO_WHATSAPP:
        if not dados.texto:
            raise ValueError("a saída no WhatsApp precisa do texto da política")
        return MensagemPronta(dados.texto)
    if tipo is TipoFixa.CONFIRMACAO:
        return MensagemPronta(_confirmacao(dados))
    if tipo is TipoFixa.CONFIRMACAO_CONVITE_CARTAO:
        if dados.link_cartao is None:
            raise ValueError("o convite para o cartão precisa do link_cartao")
        # Dois assuntos na mesma mensagem, porque cada mensagem custa (D37).
        texto = (
            f"{_confirmacao(dados)} Vimos que você paga pelo Pix todo mês. Para não se "
            "preocupar mais com isso, cadastre o seu cartão no link abaixo."
        )
        return MensagemPronta(texto, [Botao("url", "Cadastrar o cartão", dados.link_cartao)])
    raise ValueError(f"mensagem fixa desconhecida: {tipo}")
