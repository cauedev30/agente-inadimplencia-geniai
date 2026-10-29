# Número WAHA como canal de atendimento humano

> **Status: provável, não implementada (2026-10-29).** O Cauê deve seguir com ela, mas só implementa
> quando for decidir de vez, para não refazer a etapa 7 à toa. Quando virar decisão, revisa a D32 (o
> webchat) e o texto da 6ª mensagem (D19). Pergunta aberta na C6 de [`duvidas.md`](duvidas.md).

## Por que mudar

A maior objeção ao webchat ([`ideia-webchat.md`](ideia-webchat.md)), levantada por um dev da GeniAI,
é o próprio link. A conversa é sobre o pagamento de quem está devendo, e um link nessa hora gera
suspeita. O Cauê acha que o link do webchat não vai converter.

## A ideia

A 6ª mensagem manda o cliente para **um segundo número de WhatsApp, fora da API oficial, ligado pelo
WAHA**. A conversa continua **pelo Chatwoot**, como uma inbox WAHA. É o mesmo desenho do chatbot de
atendimento da GeniAI, que já roda numa inbox WAHA no Chatwoot, onde o contato privado chega com o
telefone.

Quando o cliente escreve para esse número:

1. o backend recebe a mensagem, procura o **telefone** dele entre as cobranças abertas e acha a
   conversa do número oficial;
2. deixa na conversa nova uma **nota privada com o resumo**: o que ele quer tratar, a dívida, as
   propostas feitas, por que procurou o outro número e **por que o agente não resolveu** (o laudo do
   Jev, que a nota de handoff já tem);
3. amarra as duas conversas, como a etapa 7 já faz com o webchat.

## O que muda em relação ao webchat

- **Sem página e sem link.** O cliente continua no WhatsApp e é notificado quando o atendente
  responde. Some o ponto mais fraco do webchat, a resposta que não chega.
- **Sem token e sem pedir os 3 dígitos do CPF.** O telefone identifica o cliente.
- **Continua de graça,** porque o WAHA não passa pela Meta.
- **O descarte de 2026-10-21 não vale mais.** O "outro número fora da API oficial" foi descartado
  porque o contexto não ficava salvo e a auditoria era ruim. Com o WAHA no Chatwoot, a conversa fica no
  sistema, com o histórico e os relatórios.

## Riscos

1. **"Fale com a gente neste outro número" é o roteiro do golpe mais comum.** Um número novo pode
   assustar tanto quanto um link. Atenuantes:
   - a 6ª mensagem vai como **cartão de contato** (o tipo `contacts` da API oficial), não como link;
   - o número WAHA tem o nome e a foto da unidade;
   - o atendente abre já citando o que foi falado.
2. **Banimento.** O risco é menor porque o número só recebe (quem escreve é o cliente), mas não é zero:
   inadimplente denuncia. O número é da marca Cartão de Todos, então o gestor precisa saber.
3. **O cliente escreve de outro telefone.** O sistema não acha a cobrança, e o atendente pede o CPF ou
   o nome.
4. **Retrabalho.** A etapa 7 (o token, a página do CPF, o link na 6ª) fica parada; a amarração das
   conversas e a nota de handoff se aproveitam.

## Alternativa registrada

**O humano no próprio número oficial, para todo mundo**, como a D44 já faz com quem desconfia de golpe.
É a que deve converter melhor e não tem risco de banimento. O preço é pagar à Meta as mensagens do
humano, que eram o motivo nº 1 do webchat.

## Para decidir

1. O motivo de tirar o cliente do número oficial é o **custo** das mensagens do humano na Meta? Se o
   custo for aceitável, a alternativa acima é mais simples.
2. **Quem fornece o número** (o chip e o aparelho ou a sessão WAHA) e em qual Chatwoot ele entra (T3)?
3. A franquia e o gestor aceitam o **risco de banimento** de um número da marca?
