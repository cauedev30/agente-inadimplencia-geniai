# Roteiro do teste manual

O teste manual, com roteiro, é do Cauê, no celular, no número do teste. O Luciano e a Lohanny fazem um
**teste livre**: conversam como clientes e perguntam o que quiserem, sem roteiro, comando nem meta de casos.
Eles começam com "Oi" e o telefone já nasce no caso 1 (Ana).

Os passos e as frases são os do ensaio automático (`ferramentas/demo-whatsapp/roteiros/teste.yaml`; a conversa do
desconto, a do `autonomia.yaml`).

## Como rodar

- **Uma mensagem por vez.** Espere a resposta antes de mandar a próxima: as mensagens enviadas em até 7 s
  viram uma só.
- **Mais de 2 minutos sem resposta é falha.** Anote o passo e a hora.
- **Cada caso começa com `/caso <n>`**, que recomeça o caso do zero. Depois de `/caso`, o 1º "Oi" recebe só a
  abertura.
- **Para anotar, use `/nota`** (ex.: `/nota caso 3, passo 3: veio R$ 150,00`). A nota fica presa ao caso em que
  você está, então mande antes de trocar de caso.
- **Depois do handoff o agente fica mudo de propósito.** A mensagem do handoff diz que um atendente da unidade vai
  continuar. Para seguir, mande `/reset` e depois "Oi".

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

**C. A conversa do desconto** (a que o Cauê mandou em 2026-11-17: o agente responde à dúvida em vez de passar ao
atendente). É o roteiro `desconto-so-no-cartao` do ensaio (`ferramentas/demo-whatsapp/roteiros/autonomia.yaml`).

| # | Envie | Esperado | É falha se |
|---|---|---|---|
| C1 | `/caso 1` | `[TESTE] Agora você é o caso 1: R3, pagava no cartão. Mande Oi para começar.` | outro caso |
| C2 | `Oi` | a abertura da Ana, com R$ 100,20 | sem resposta |
| C3 | `Tem desconto?` | a oferta de R$ 70,20, que só vale pelo link (`[TESTE]`) | outro valor; vai ao atendente |
| C4 | `Então esse desconto só existe se eu cadastrar meu cartão?` | uma resposta do agente: o desconto de R$ 70,20 vale pelo link, que cadastra o cartão, e sem cadastrar o valor é o integral, R$ 100,20; termina no próximo passo para pagar | vai ao atendente; sem resposta em 2 min; outro valor |
| C5 | `Não quero cadastrar cartão` | uma resposta que reconhece o que você disse e o Pix de R$ 100,20 com a chave `29760112000122` (não pague) | sem a chave; outro valor; vai ao atendente |

**D. Frases livres e em rajada.** O agente agora responde ao que o cliente escreve, então vale sair do roteiro:

- **Frases livres:** depois dos passos acima, mande 3 a 5 frases suas, do jeito que um cliente escreve: uma dúvida sobre
  a cobrança ("o valor do Pix é o mesmo do link?"), um desabafo ("tô desempregado"), um "Oi" no meio da conversa, um
  assunto de fora ("que horas a clínica abre?"). Cada uma é uma mensagem, esperando a resposta. Anote com `/nota` a
  resposta que ficou estranha.
- **Em rajada:** mande 2 ou 3 mensagens seguidas em menos de 7 s ("Oi", "quanto eu devo?", "tem desconto?"). Elas viram um
  turno só e recebem uma resposta só. É falha se vierem respostas separadas ou se uma das perguntas ficar sem resposta.
- **Em toda resposta do agente,** confira: os valores são os da tabela dos casos; a resposta leva o Pix ou o link, ou
  aponta o que ele já mandou; não promete prazo, perdão nem "vou verificar"; não fala de corte nem de cancelamento.

## O que ainda não funciona (não é defeito)

- **O Pix traz a chave CNPJ real, mas não é para pagar:** nada confere o pagamento, e só o `/paguei` confirma.
  **O link do cartão é de mentira** e vem com `[TESTE]`, porque o sistema do cartão (T9) ainda não está ligado.
- **Foto e PDF sem texto vão ao atendente**, para ele conferir o comprovante. **Figurinha** fica sem resposta.
- **Quem cobra é o agente.** Ele só responde ao que chega, não puxa conversa sozinho, e o disparador (T4) ainda não
  existe.

## O que o agente responde e o que vai ao atendente (não é defeito)

O agente responde a tudo o que é da cobrança, e toda resposta leva ao pagamento. O resto vai ao atendente.

**O agente responde sozinho** (o Luna escreve, com valores do sistema):
- o valor, o desconto e a forma de pagar ("quanto devo?", "tem desconto?", "o desconto só vale no cartão?", "no Pix tem
  desconto?", "esse valor é de quantas mensalidades?", "como funciona o pagamento pelo link?");
- uma dúvida ou um comentário sobre a cobrança: "você é um robô?" (é o assistente virtual do Cartão de Todos), "você é
  humano?", "posso pagar semana que vem?" (pode pagar quando puder, pedindo o Pix ou o link de novo; o agente não
  registra promessa nem segura prazo), "dá pra parcelar?" (não), "como é o processo de pagamento?";
- a dificuldade de pagar ("tô desempregado, não consigo pagar tudo"): acolhe em uma frase e oferece o desconto;
- "Não quero cadastrar cartão": responde e manda o Pix do valor integral;
- "Oi" e "bom dia" no meio da conversa: cumprimenta e segue de onde parou;
- um assunto que não é a mensalidade (horário, endereço, clínica): uma frase que volta ao pagamento, sem afirmar nada
  sobre o assunto.

**Fica sem resposta:** "Ok" e "obrigado" (só confirmação ou agradecimento), e a figurinha.

**Recebe uma frase pronta:** quem não é o titular ("sou o marido dela").

**Vão ao atendente, com uma mensagem que o Luna escreve** (uma ou duas frases que acolhem o que você disse e avisam que um
atendente da unidade continua na conversa; sem número, sem promessa):
- "já paguei" e "não devo isso";
- "quero cancelar o cartão" e "quero pagar na conta de energia";
- uma reclamação da clínica;
- uma situação grave (doença, luto, acidente);
- o pedido de uma pessoa ("quero falar com um atendente", "falar com alguém");
- quem está irritado ou xinga;
- uma pergunta que o agente não sabe responder ("quais meses estão em aberto?", vencimento, boleto).

**Vão ao atendente, com a frase pronta, sem chamar o Luna:** "Procon", "advogado", "vou processar" e "isso é golpe" (a
frase do golpe é a dela).

**Foto ou PDF sem texto** vão ao atendente com *"Recebi o seu arquivo. Um atendente da unidade vai conferir…"*.

**O teto:** o agente manda no máximo 5 mensagens de resolução; a 6ª é fixa e passa a conversa ao atendente. A mensagem do
handoff não conta.

É falha se o handoff vier com valor, link ou sem a palavra "atendente" (o código devia ter trocado pela frase pronta).
Promessa ("vou verificar", "já te retorno") o código não confere: anote com `/nota`.

No teste livre do Luciano e da Lohanny, o handoff vai acontecer sempre que o cliente cair num sinal de saída, pedir uma pessoa, ficar irritado ou hostil ou fizer uma pergunta sem resposta; o fora do assunto o Luna responde. Na
produção seria igual; a diferença é que, no teste, ninguém responde depois do handoff.
