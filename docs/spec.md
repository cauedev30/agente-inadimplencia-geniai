# Spec do produto

Status: em aberto. Atualizado em 2026-10-19.

As decisões citadas (D1, D2, …) estão em [`decisoes.md`](decisoes.md). O que ainda falta definir está
em [`duvidas.md`](duvidas.md).

## 1. O problema

As franquias do Cartão de Todos usam o disparador em massa da GeniAI, que roda pela API oficial do
WhatsApp (Meta), para cobrar clientes inadimplentes. Hoje a negociação que vem depois do disparo é
feita por atendentes humanos.

Desde 1º de outubro de 2026, a Meta passou a cobrar também as respostas enviadas dentro da janela de
24 horas, que antes eram de graça. O preço é o da mensagem de utilidade do país, e cada número tem
1.000 respostas grátis por mês. Fontes:
[Landbot](https://landbot.io/blog/whatsapp-business-api-pricing-change-october) e
[Telnyx](https://telnyx.com/resources/whatsapp-business-api-cost), consultadas em 2026-10-15. Falta
confirmar no Billing Hub de um cliente.

Com isso, **cada mensagem de resposta custa dinheiro**. O número de mensagens até o pagamento passa a
ser uma métrica de custo, e não só de experiência do cliente.

## 2. O que o agente faz

- Substitui o atendimento humano na cobrança. A meta é ser mais assertivo que o humano.
- Tenta resolver o pagamento no menor número de mensagens. Se o cliente já chega disposto, uma
  mensagem só, com o Pix do valor, pode bastar.
- Negocia dentro da política da franquia: desconto, parcelamento etc. Ver a dúvida G1.
- Passa a conversa para um humano quando o caso não é para o robô. Exemplos: o cliente quer cancelar,
  cita Procon ou advogado, diz que já pagou, contesta a dívida ou está em situação vulnerável. Os
  detalhes estão em [`arquitetura.md`](arquitetura.md).

## 3. Escopo da primeira versão

- **Uma franquia: Cartão de Todos João Pessoa Sul**, em uso real (D17). Cada franquia vai ter as suas
  credenciais, separadas (D13). A expansão para outras franquias vem se o projeto der certo.
- **Serviço próprio**, ligado ao Chatwoot como Agent Bot (D1).
- **A carteira de inadimplentes é importada no próprio agente** (D2).
- **O agente dispara a mensagem de abertura**, a que cita a dívida, pela API do disparador (D3).
- **Pagamento por Pix dinâmico**, gerado por um PSP com webhook de confirmação, caindo na conta da
  franquia (D4).

## 4. Regras da conversa

| Regra | Decisão |
|---|---|
| **Teto de resolução: 3 mensagens do agente.** Vale para todas as franquias | D8, D14 |
| **A 4ª mensagem é fixa, sem LLM.** Manda o cliente para o canal externo da franquia, que é um web chat ou outro número de WhatsApp, com o texto: *"Para o problema ser resolvido de forma mais eficaz, entre em contato conosco por esse canal de atendimento."* | D5, D18, D19 |
| **Janela de atendimento:** segunda a sexta, das 8h às 18h; sábado, das 8h às 12h. Domingo e feriado ficam fora | D11, D16 |
| **Fora da janela, o agente não responde nada**, porque cada mensagem custa. O que chegou fora do horário é respondido quando a janela abre, como se tivesse chegado no horário. Os atendentes também veem essas mensagens no horário deles | D16 |
| **Toda saída do robô vai para o canal externo da franquia**, seja pelo teto ou por um gatilho do Jev | D5 |

## 5. Fora do escopo por enquanto

- Várias franquias ao mesmo tempo (D13, D17).
- Boleto: o desenho prevê `GENERATE_BOLETO` como não implementado, e o caso vai para o humano.

## 6. Critério de aceite

A definir. Ver a dúvida G4 em [`duvidas.md`](duvidas.md).
