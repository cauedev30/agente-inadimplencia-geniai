# Roteiro do teste manual

O teste manual, com roteiro, é do Cauê, no celular, no número do teste. O Luciano e a Lohanny fazem um
**teste livre**: conversam como clientes e perguntam o que quiserem, sem roteiro, comando nem meta de casos.
Eles começam com "Oi" e o telefone já nasce no caso 1 (Ana).

Os passos e as frases são os do ensaio automático (`ferramentas/demo-whatsapp/roteiros/teste.yaml`).

## Como rodar

- **Uma mensagem por vez.** Espere a resposta antes de mandar a próxima: as mensagens enviadas em até 7 s
  viram uma só.
- **Mais de 2 minutos sem resposta é falha.** Anote o passo e a hora.
- **Cada caso começa com `/caso <n>`**, que recomeça o caso do zero. Depois de `/caso`, o 1º "Oi" recebe só a
  abertura.
- **Para anotar, use `/nota`** (ex.: `/nota caso 3, passo 3: veio R$ 150,00`). A nota fica presa ao caso em que
  você está, então mande antes de trocar de caso.
- **Depois do handoff o agente fica mudo de propósito.** A frase do handoff diz que um atendente vai continuar.
  Para seguir, mande `/reset` e depois "Oi".

Cada mensalidade é R$ 33,40.

| Caso | Cliente | Atraso | Dívida |
|---|---|---|---|
| 1 | Ana, pagava no cartão | 3 meses | R$ 100,20 |
| 2 | Bruno, paga Pix todo mês (tende ao cartão) | 3 meses | R$ 100,20 |
| 3 | Carla, pagava boleto | 4 meses | R$ 133,60 |
| 4 | Daniel, histórico misto | 5 meses | R$ 167,00 |
| 5 | Elisa, último mês antes do corte | 6 meses | R$ 200,40 |

## Os 5 casos

Os cinco casos têm os mesmos 5 passos. Troque `<n>`, `<nome>` e `<dívida>` pelos valores da tabela acima.

| # | Envie | Esperado | É falha se |
|---|---|---|---|
| 1 | `/caso <n>` | `[TESTE] Agora você é o caso <n>: ... Mande Oi para começar.` | aparece CPF; vem outro número de caso |
| 2 | `Oi` | a abertura: "Oi, `<nome>`", com a `<dívida>` | outro nome ou outro valor; nenhuma resposta |
| 3 | `Quanto eu estou devendo?` | a `<dívida>` exata | outro valor; "com desconto" sem citar o valor do desconto; vai ao atendente |
| 4 | `Pagar com Pix` | o Pix de `<dívida>` com a chave `29760112000122` (não pague) | outro valor; sem a chave; vai ao atendente |
| 5 | `/paguei` | "Simulando a confirmação do banco: Pix de R$ `<dívida>`" e, alguns segundos depois, "Recebemos o seu pagamento" | a confirmação não chega em 2 min; chega 2 vezes; o valor muda |

No **caso 2**, o link do cartão (`[TESTE]`) pode vir antes do Pix, porque o Bruno tende ao cartão. Isso não é
falha: siga para o passo 4.

## Depois dos 5 casos (2 min cada)

**A. O áudio.** No `dev2` a transcrição está ligada desde 2026-11-16 (D43): o agente ouve o áudio e responde
como se fosse texto. Fale uma frase inteira, devagar; um áudio de 2 s pode ser mal ouvido e ir ao atendente.

| # | Envie | Esperado | É falha se |
|---|---|---|---|
| A1 | `/caso 1` | `[TESTE] Agora você é o caso 1: R3, pagava no cartão. Mande Oi para começar.` | outro caso |
| A2 | `Oi` | "Oi, Ana! Vi 3 mensalidades em aberto, R$ 100,20 no total." | outro valor; sem resposta |
| A3 | **áudio** de uns 5 s: "Oi, eu queria saber quanto que eu estou devendo" | uma resposta com R$ 100,20 | vai ao atendente; sem resposta em 2 min |

**B. Resolver no Chatwoot e escrever de novo.**

| # | Faça | Esperado | É falha se |
|---|---|---|---|
| B1 | `/caso 1` e depois `Oi` | a abertura da Ana com R$ 100,20 | sem resposta |
| B2 | No Chatwoot (inbox 280), abra a conversa e clique em **Resolver** | a conversa some da lista de abertas | — |
| B3 | No celular: `Quanto eu estou devendo?` | R$ 100,20 | sem resposta em 2 min |
| B4 | Olhe o Chatwoot | nasce outra conversa (a resolvida continua resolvida), e o agente responde nela | a mesma conversa reabre |

## O que ainda não funciona (não é defeito)

- **O Pix traz a chave CNPJ real, mas não é para pagar:** nada confere o pagamento, e só o `/paguei` confirma.
  **O link do cartão é de mentira** e vem com `[TESTE]`, porque o sistema do cartão (T9) ainda não está ligado.
- **Foto e PDF sem texto vão ao atendente**, para ele conferir o comprovante. **Figurinha** fica sem resposta.
- **Quem cobra é o agente.** Ele só responde ao que chega, não puxa conversa sozinho, e o disparador (T4) ainda não
  existe.

## O que vai ao atendente de propósito (não é defeito)

O agente só resolve sozinho o que leva ao pagamento: o valor, o desconto, o link e o Pix. O resto tem saída fixa:
- **Vão ao atendente, com uma frase pronta:**
  - uma pergunta que ele não sabe responder ("quais meses estão em aberto?");
  - o "já paguei";
  - um desabafo;
  - o pedido de uma pessoa;
  - "golpe", "advogado" ou "Procon".
- **Recebe uma frase pronta:** um assunto que não é a mensalidade (horário, endereço, clínica).
- **Fica sem resposta:** "Ok" e "obrigado".
- **O teto:** o agente manda no máximo 5 mensagens de resolução; a 6ª é fixa e passa a conversa ao atendente.

No teste livre do Luciano e da Lohanny, isso vai acontecer bastante. Na produção seria igual; a diferença é que,
no teste, ninguém responde depois do handoff.
