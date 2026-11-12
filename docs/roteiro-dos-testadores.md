# Teste do agente de inadimplência

Para o Luciano, a Lohanny e o Cauê. O teste dura até 3 dias úteis; converse quando puder e quantas vezes
quiser. Não há passo a passo: vocês conhecem os clientes e os problemas da operação melhor do que qualquer
roteiro.

## O que é

Você conversa pelo WhatsApp com o **agente de inadimplência**, como se fosse um cliente do Cartão de Todos
com mensalidades em atraso. O agente lê, responde, informa a dívida e oferece o Pix.

- **Os clientes são inventados** (nome e CPF de mentira). Ninguém é cobrado, e nenhum dado real entra.
- **O Pix e o link do cartão são de mentira** (aparecem com `[TESTE]`). A conversa é de verdade: o agente
  usa a inteligência artificial de verdade para entender você e redigir as respostas.
- **Só os 3 telefones do teste conversam com ele.** Qualquer outro número é ignorado.

## Como usar

Mande **`Oi`** para o número do teste. A primeira resposta diz quem você é (o caso 1), a dívida e um CPF
fictício, que você usa se o agente pedir. A partir daí, converse como um cliente de verdade: do jeito mais
difícil que você já viu na operação.

Para pedir o Pix, escreva **`Pagar com Pix`** ou peça com as suas palavras.

| Comando | O que faz |
|---|---|
| `/caso` | Lista os 5 casos. |
| `/caso 3` | Você passa a ser o caso 3 (troque o número). |
| `/paguei` | Simula o banco confirmando o Pix que o agente deu. |
| `/nota texto livre` | Anota uma observação sua. É como você devolve o retorno. |

Também existem `/reset` (recomeça o caso em que você está) e `/paguei 2` (simula 2 mensalidades pagas por
fora, sem passar pelo agente).

Mande uma mensagem por vez e espere a resposta (ele leva alguns segundos). Pode testar **a qualquer hora**.
Se ficar mais de 2 minutos sem resposta, avise o Cauê.

## O que pedimos

**Passe pelos 5 casos pelo menos uma vez**, cada um do seu jeito. O resto é livre.

| Caso | Cliente | Atraso | Dívida |
|---|---|---|---|
| 1 | Ana, pagava no cartão | 3 meses | R$ 100,20 |
| 2 | Bruno, paga Pix todo mês (tende ao cartão: o link do cartão vem antes do Pix) | 3 meses | R$ 100,20 |
| 3 | Carla, pagava boleto | 4 meses | R$ 133,60 |
| 4 | Daniel, histórico misto | 5 meses | R$ 167,00 |
| 5 | Elisa, último mês antes do corte | 6 meses | R$ 200,40 |

Cada mensalidade é R$ 33,40; o valor que o agente cita tem de bater com a coluna "Dívida".

## O que queremos saber

- Ele errou algum valor?
- Soou frio, insistente, confuso ou robótico em algum momento?
- Isso é como a equipe falaria com um cliente? Causaria problema com um cliente real?
- Ele entendeu o que o cliente quis dizer? Deixou de entender algo que um cliente de verdade diria?
- O que um cliente real faria que ele ainda não sabe tratar?

Se o ajudar a lembrar de situações, algumas ideias (todas opcionais): reclamar do valor, dizer que já pagou,
pedir uma pessoa, desconfiar de golpe, mandar várias mensagens seguidas, escrever errado ou de forma curta.
Lembrete: o agente manda no máximo 5 mensagens de resolução; na seguinte vem uma sexta, fixa, passando a conversa
para um atendente. Se algum **desconto** oferecido parecer estranho, anote: o desconto máximo de cada faixa é uma
dúvida ainda aberta com a gestão, e o teste usa o padrão da spec.

## Como mandar o retorno

Use **`/nota`** na própria conversa, logo depois de ver o que quer comentar. O teste responde "Nota anotada" e
guarda a nota junto com o que estava acontecendo. Elogio também vale.

```
/nota caso 3: ele falou R$ 150,00 em vez de R$ 133,60
/nota caso 1: a resposta ficou longa, repetiu o valor 3 vezes
/nota caso 5: gostei, o tom ficou firme sem ser agressivo
```

Ajuda dizer o número do caso, o que você escreveu e o que esperava. Se algo travar, ficar mudo ou parecer
perigoso (dado de outra pessoa, valor absurdo), **avise o Cauê direto**, além da nota.

## O que ainda não funciona (não é defeito)

- **Foto, figurinha, áudio e PDF:** o agente não lê e fica mudo. Escreva em texto.
- **Pix e link do cartão:** vêm como `[TESTE] aqui viria...`, porque o Pix real e o sistema do cartão ainda não
  estão ligados.
- O agente só responde ao que você escreve; ele não puxa conversa sozinho.
