# Roteiro dos testadores

Para o Luciano, a Lohanny e o Cauê. Leva uns 30 minutos por pessoa; o teste dura até 3 dias úteis.

## O que é este teste

Você conversa pelo WhatsApp com o **agente de inadimplência**, como se fosse um cliente do Cartão de Todos
com mensalidades em atraso. O agente lê o que você escreve, responde, informa a dívida e oferece o Pix.

- **Tudo é inventado.** Os 5 casos são clientes fictícios (nome e CPF de mentira). Nenhum dado de cliente
  real entra, e nenhum dinheiro de verdade é cobrado.
- **O que é de mentira:** o código Pix (aparece com `[TESTE]`), o link do cartão e o "pagamento", que você
  simula com um comando. Nada disso vai a banco nenhum.
- **O que é de verdade:** a conversa. O agente usa a inteligência artificial de verdade para entender você e
  redigir as respostas. É isso que queremos avaliar: o tom, os valores, a ordem do que ele diz.
- **Só conversa com os 3 telefones do teste.** Outro número é ignorado, sem resposta.

## Horário

Pode conversar a **qualquer hora**, inclusive à noite e no fim de semana: no teste, a janela de atendimento está
aberta o dia todo. (No uso real o agente só responde de segunda a sexta, 8h às 18h, e sábado, 8h às 12h.)

A resposta leva alguns segundos. Ele espera cerca de 7 segundos para juntar mensagens seguidas, então **mande
uma mensagem por vez e espere a resposta**. Se passar de **2 minutos** sem resposta, avise o Cauê.

## O que não mandar

**Foto, figurinha, áudio e PDF.** O agente ainda não lê imagem nem ouve áudio: ele fica mudo (é esperado, não
é defeito). Escreva em texto.

## Os comandos

Os comandos começam com `/`. Quem responde a eles é o teste, e a resposta vem marcada com `[TESTE]`.

| Comando | Para que serve |
|---|---|
| `/caso` | Lista os 5 casos. |
| `/caso 3` | Passa a ser o caso 3 (troque o número). Recomeça a conversa dele. |
| `/paguei` | Simula o banco confirmando o Pix (ou o link) que o agente pediu. |
| `/paguei 2` | Simula que você pagou 2 mensalidades por fora, sem passar pelo agente. |
| `/nota texto livre` | Anota uma observação sua para a equipe. **É assim que você nos devolve o retorno.** |
| `/reset` | Recomeça o caso em que você está, do zero. |

Se escrever um comando que não existe, o teste responde com a lista.

## Como começar

1. Mande **`Oi`** para o número do teste. A primeira resposta diz "Você é o caso 1", a dívida do caso e o CPF
   fictício. Se o agente pedir seu CPF, use esse.
2. Converse como um cliente de verdade: pergunte quanto deve, reclame do valor, peça o Pix, diga que já pagou,
   peça para falar com uma pessoa.
3. Para pedir o Pix, escreva **`Pagar com Pix`** (no teste não há botão) ou peça com as suas palavras.
4. Para terminar um caso, mande **`/paguei`** depois que o agente der o Pix.
5. Para mudar de caso, **`/caso <n>`**. Depois que um caso termina, o teste avisa e pede um novo `/caso`.

## Os 5 casos

Cada mensalidade é R$ 33,40. O valor que o agente cita tem de bater com a coluna "Dívida".

| Caso | Quem é | Meses em atraso | Dívida | O que observar |
|---|---|---|---|---|
| 1 | Ana, R3, pagava no cartão | 3 | R$ 100,20 | O valor está certo? Ele oferece o Pix? |
| 2 | Bruno, R3, paga Pix todo mês | 3 | R$ 100,20 | É o único que tende ao cartão: o agente deve trazer primeiro o link do cartão (`[TESTE]`), e a confirmação do pagamento convida a cadastrar o cartão. Os outros recebem o Pix primeiro. |
| 3 | Carla, R4, pagava boleto | 4 | R$ 133,60 | O valor muda para 4 meses? |
| 4 | Daniel, R5, histórico misto | 5 | R$ 167,00 | Idem, com 5 meses. |
| 5 | Elisa, R6, último mês antes do corte | 6 | R$ 200,40 | A conversa deixa claro que é o último prazo, sem ameaça nem exagero? |

Em todos, faça o caminho completo: `Oi` → "Quanto eu estou devendo?" → `Pagar com Pix` → `/paguei`. Depois
repita com outra conversa, mais difícil.

## O que testar além do caminho feliz

Em qualquer caso, escolha algumas situações e veja se a resposta é boa:

- **Reclamar do valor** ("está caro", "não tenho como pagar tudo"). O agente pode oferecer desconto em até 3
  degraus, um por reclamação. Se o valor oferecido lhe parecer estranho, **anote com `/nota`**: o desconto máximo
  de cada faixa ainda é uma dúvida aberta com a gestão (G1.1), e o número do teste segue o padrão da spec.
- **Dizer que já pagou** ("já paguei isso semana passada"). O esperado é uma mensagem de encaminhamento, sem
  cobrar de novo, e a conversa vai para um atendente conferir.
- **Pedir um atendente** ("quero falar com uma pessoa"). A conversa passa ao atendente. No teste ninguém
  responde por ele; o que vale é a mensagem do agente e o aviso aparecer na conversa.
- **Desconfiar** ("isso é golpe?"). O agente deve acalmar e passar a um atendente, sem insistir.
- **O limite de mensagens.** O agente manda no máximo **5 mensagens** de resolução; na seguinte vem uma sexta,
  fixa: "Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui mesmo, nesta
  conversa." Para ver isso, peça o Pix várias vezes seguidas, esperando cada resposta.
- **Pagar parte por fora:** no caso 3, `/paguei 2` simula 2 mensalidades pagas fora do agente. Fica 2 em aberto
  (R$ 66,80); pergunte "quanto eu estou devendo?" e veja se o agente cita o valor novo.

## Como mandar o retorno

Use **`/nota`** na própria conversa, logo depois de ver o problema. O teste responde "Nota anotada" e guarda a
nota junto com o caso e o que estava acontecendo. Escreva:

```
/nota caso 3: ele falou R$ 150,00 em vez de R$ 133,60
/nota caso 1: a resposta ficou longa demais, repetiu o valor 3 vezes
/nota caso 5: gostei, o tom ficou firme sem ser agressivo
```

O que ajuda: o **número do caso**, **o que você mandou**, **o que o agente respondeu** e **o que você esperava**.
Elogio também vale. Se algo travar, ficar mudo ou parecer perigoso (dado de outra pessoa, valor absurdo),
**avise o Cauê direto**, além da nota.

## Limites do teste (não é defeito)

- O código Pix e o link vêm como `[TESTE] aqui viria...`: o Pix real depende do banco e o link, do sistema do cartão.
- Foto, figurinha e áudio não são lidos.
- O agente só reage quando você escreve; ele não puxa conversa sozinho.
- Se o `/caso` ou o `/paguei` não responder, espere o fim dos 7 segundos e tente de novo uma vez.
