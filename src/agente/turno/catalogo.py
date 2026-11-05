"""Mensagens fixas: sem LLM e sem Jev, com os dados preenchidos pelo backend (D33, D19).

Os textos de confirmação são provisórios: o projeto desenha os finais (D28).
"""

from dataclasses import dataclass, field

from agente.dominio.dinheiro import formatar_reais
from agente.tipos import Botao, TipoFixa

# D19: o texto do canal externo é fixo.
TEXTO_CANAL_EXTERNO = (
    "Para o problema ser resolvido de forma mais eficaz, entre em contato conosco "
    "por esse canal de atendimento."
)


# A resposta segura do turno em que o Jev 3 reprovou o LLM duas vezes (seção 6). Sem número nem
# promessa: provisória, como as confirmações (D28).
TEXTO_RESPOSTA_SEGURA = (
    "Desculpe, não consegui te responder direito agora. Pode me contar de novo o que você precisa?"
)


@dataclass(frozen=True)
class DadosFixa:
    nome: str
    valor_centavos: int | None
    link_cartao: str | None
    link_canal_externo: str
    # O texto que a política dá à fixa que não tem texto próprio (PEDIR_TEXTO e HUMANO_NO_WHATSAPP).
    # No CANAL_EXTERNO, o texto redigido pelo LLM no turno de uma chamada (D47), seguido da frase da
    # D19; sem ele, só a da D19.
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
        botao = Botao("url", "Falar com um atendente", dados.link_canal_externo)
        # O texto redigido pelo LLM abre a mensagem e a frase fixa da D19 fecha, porque é ela que
        # manda o cliente tocar no botão (o atendente está atrás dele); o LLM não sabe o canal
        # (D47, I2 da revisão da E1).
        texto = f"{dados.texto}\n\n{TEXTO_CANAL_EXTERNO}" if dados.texto else TEXTO_CANAL_EXTERNO
        return MensagemPronta(texto, [botao])
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
