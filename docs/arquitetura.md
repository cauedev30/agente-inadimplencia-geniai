# Arquitetura do agente

Reescrita em 2026-11-11 (D59, D60, D61) e revista em 2026-11-17 (o Luna responde a tudo o que é da cobrança e redige a
passagem ao atendente; revisa a D63, ver [`politicas.md`](politicas.md)). Saíram do código e ficam na tag `antes-do-enxugamento`: o pipeline do Jev
(leitura, decisão e validação), a simulação e a comparação de redatores.
Sem efeito pelas três decisões: o modo sombra e o webchat.
Onde esta página e as decisões divergirem, valem as decisões ([`decisoes.md`](decisoes.md)). A
numeração das seções segue a das versões anteriores, porque os comentários do código as citam (seção 1: gates; 3:
estados; 4: ações; 5: backend; 9: falhas).

## Princípio

**O Luna lê e escreve; o código confere e executa.**

- O **Luna** (`gpt-6-luna`, D42) lê a conversa e devolve, numa chamada só, os sinais da mensagem do cliente, a ação do
  turno e o texto da resposta (D59). O texto vale nas ações com texto (as 4 de cobrança, `ANSWER` e `OUT_OF_SCOPE`) e
  na **passagem ao atendente**, que ele redige quando um sinal ou o tom manda ao humano (D63, revista em 2026-11-17: o
  Luna responde a tudo o que é da cobrança, e toda resposta leva ao pagamento). Não recebe nenhum valor em dinheiro,
  link nem código Pix: escreve **marcadores** (`{divida}`, `{valor_final}`, `{link_ctn}`, `{pix_copia_e_cola}`…).
- O **backend** faz as contas e executa: a dívida (D24), o degrau do desconto (D35), a propensão ao cartão (D36), o Pix e
  o link do CTN. Troca cada marcador pelo valor certo.
- O **código** confere tudo o que o Luna devolveu: a ação é permitida, os marcadores são os da ação, o texto não traz
  dígito nem valor solto, a passagem não traz número, link nem marcador, o cliente sai da resposta com um meio de
  pagamento vigente, os gatilhos de humano e a guarda de palavra-chave vencem o texto, e o teto vale (D33).

## O turno, em uma linha

```
webhook do Chatwoot → rajada (7 s) → gates → [áudio: transcreve] → uma chamada ao Luna
→ validação pelo código → backend executa (degrau, link, Pix) → envio pelo Chatwoot  |  handoff
```

## 1. Entrada e gates (determinísticos)

São certezas, não probabilidades, e por isso ficam em código.

1. **Webhook** (`POST /webhooks/chatwoot/{segredo}`). Confere o segredo, o JSON, a conta e a inbox e descarta o evento
   repetido (`message_id`). A mensagem do cliente é gravada e agenda o turno para daqui a `rajada_segundos` (7 s); cada
   nova mensagem empurra o turno, que junta a rajada inteira. A mensagem de um atendente grava e marca a conversa como
   do humano. A mudança de status para Pendente devolve a conversa ao robô. No modo teste, o primeiro contato de um
   testador do piloto cria o caso padrão.
2. **Piloto.** O turno de quem está fora da lista de telefones do piloto (`liberacao`) é consumido e ignorado, sem Luna,
   sem CTN e sem mensagem (D61).
3. **Comandos do testador** (modo teste): `/caso`, `/paguei`, `/nota` e o reinício são tratados antes de tudo, nunca
   chegam ao Luna e ficam fora do teto.
4. **Conferência no CTN.** Com o CTN real (e nos casos do modo teste) e uma vez por dia por cobrança, os lançamentos e as
   baixas do cliente confirmam os meses e o valor exatos antes de qualquer valor ser citado (D49). Sem conferência
   válida, o turno espera como se a integração estivesse fora do ar.
5. **Gates**, nesta ordem (`turno/gates.py`):
   - cobrança ativa;
   - reinício (só telefones da equipe);
   - humano na conversa;
   - anexo que o agente não lê;
   - só figurinha (`[Sticker]`): silêncio, sem chamar o Luna (D63);
   - **janela de atendimento**: segunda a sexta, 8h às 18h; sábado, 8h às 12h; domingo e feriado fora (D11, D16). Fora
     dela nada é enviado e o turno volta quando ela abre; só o toque no botão "Pagar com Pix" passa (D40);
   - **janela de 24h do WhatsApp**: passada, o humano assume com uma nota, sem mensagem ao cliente;
   - **teto de 5 mensagens**, com a 6ª fixa (D33, D60). A abertura não conta, e a confirmação de pagamento sai sempre,
     fora do teto;
   - cobrança quitada no CTN: o humano confere (`CONCILIACAO`), com a mensagem só na 1ª vez;
   - foto ou PDF sem texto, inclusive o que chega como `[Image]`: o humano confere o comprovante (`CONCILIACAO`);
   - toque no botão "Pagar com Pix": segue direto ao Pix fixo (seção 5), sem o Luna.

   Passando todos, o turno segue ao Luna.
6. **Áudio** (D43). Depois dos gates, o áudio é baixado do Chatwoot (só da origem dele, até 5 MB, até 2 min), transcrito
   pelo `gpt-4o-mini-transcribe`, uma vez, e entra no histórico como `[áudio transcrito: "…"]`; o que não se lê aparece
   como `[áudio longo demais — não ouvido]` ou `[áudio — não foi possível ouvir]`. A rajada só com áudio que não se lê
   leva uma fixa que pede o texto, fora do teto; na 2ª vez, o humano assume (`AUDIO`). Cada transcrição vira uma nota
   privada na conversa.

## 2. A chamada ao Luna e o handoff

**A chamada** (`turno/uma_chamada.py`). A entrada traz o primeiro nome, a régua, o degrau, a forma já enviada, o meio de
pagamento vigente (`meio_vigente`: o Pix ou o link que o cliente ainda pode usar, ou nulo), se ele paga Pix todo mês
(`paga_pix_todo_mes`), as ações permitidas (cada uma com "quando usar", "o que o texto faz" e os marcadores), as regras
da cobrança, o histórico e a rajada. A saída é um JSON em esquema estrito (`json_schema`) com os sinais (entre eles o
`sem_resposta`), o tom, a ação e o `reply`. O `reply` vale nas ações com texto e na passagem ao atendente, e fica vazio
no `NONE` e no terceiro; a temperatura é 0,7, fixa. Saída inválida ou ação fora do enum conta como falha de integração
(3 tentativas, depois `INTEGRACAO_INDISPONIVEL`).

**A decisão** é do código: vence a primeira regra que bate. A guarda de palavra-chave (golpe ou golpista; advogado,
Procon ou processar) vem antes da chamada e manda ao atendente **sem chamar o Luna**. "Humano", "atendente", "falar com
alguém" e "processo" não são palavra-chave: vão ao Luna, que marca o sinal. Depois da chamada: um sinal de saída, o tom
irritado ou hostil, o pedido de humano, o terceiro, o `sem_resposta`, a ação fora das permitidas e, por fim, a ação com
o que vai junto (seção 4).

| Sinal | Motivo |
|---|---|
| `quer_retencao` | `RETENCAO` |
| `risco_juridico` | `JURIDICO` |
| `alega_pagamento` | `CONCILIACAO` |
| `contesta_divida` | `CONTESTACAO` |
| `desconfia_golpe` | `DESCONFIANCA` (D44) |
| `vulnerabilidade` | `CAUTELA` |
| `atrito_clinica` | `ATRITO` |
| `quer_conta_energia` | `ENERGISA` (a troca é manual, D30) |
| `quer_humano` | `PEDIU_HUMANO` |
| `sem_resposta` (pede algo da cobrança que as regras e as ações não cobrem: quais meses, vencimento, boleto) | `SEM_RESPOSTA` |
| tom `irritado` ou `hostil` | `TOM` |

O `vulnerabilidade` é só a situação grave (doença séria, luto, acidente, crise); a falta de dinheiro comum vai ao
`OFFER_DISCOUNT` ou à `ANSWER`. O `terceiro` (quem escreve não é o devedor) não vai ao humano: recebe uma frase fixa, sem
falar da dívida (D63). As outras saídas fixas (`NONE`, cortesia, o fora do assunto reprovado) estão na tabela de
[`politicas.md`](politicas.md).

**O handoff.** Toda saída do robô passa a conversa a um atendente **na mesma conversa do WhatsApp** (D60): uma nota
privada com o motivo, a etiqueta, a atribuição ao time (`AGENTE_CHATWOOT_TIME_HUMANO`) e o status aberto. O estado vira
`ATENDIMENTO_HUMANO`. Quando há mensagem ao cliente, ela é a **passagem redigida pelo Luna** (uma ou duas frases que acolhem o que o cliente
disse e avisam que um atendente da unidade continua nesta conversa), gravada com origem `AGENTE` e fora do teto. O código
confere a passagem (`conferir_passagem`: sem número, link nem marcador, com a palavra "atendente"); reprovada, sai a
fixa *"Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui mesmo, nesta conversa."*, sem
2ª chamada. O **jurídico** e o **golpe** levam sempre a frase fixa (o golpe tem a própria, D44), assim como o `NONE`
fora da cortesia, o texto reprovado e a 6ª. A foto sem texto leva uma frase fixa própria (*"Recebi o seu arquivo. Um
atendente da unidade vai conferir…"*). A passagem não leva Pix nem link. Fica sem mensagem ao cliente a volta ao humano de quem já recebeu a 6ª ou o aviso de cobrança
quitada, e o caso das 24h vencidas. Outros motivos: `SEM_RESPOSTA`, `TETO`, `AUDIO`, `VALIDACAO`, `FORA_DAS_24H`,
`INTEGRACAO_INDISPONIVEL`, `ERRO_INTERNO` e `PAGAMENTO_DIVERGENTE`. O rodízio entre os atendentes é a atribuição automática do Chatwoot (*a confirmar* na conta do
JP Sul, D60).

## 3. Estados da conversa

`INICIADO` → `ENGAJADO` → `AGUARDANDO_PAGAMENTO` → `PAGO` → `ENCERRADO`, mais `ATENDIMENTO_HUMANO` (com motivo).

- **`AGUARDANDO_PAGAMENTO`:** o turno enviou o Pix ou o link. O Pix vale 24h (G2.4); sem pagamento, o agente não volta a
  procurar o cliente. Se o cliente escrever de novo, a conversa segue dentro do teto.
- **`PAGO`:** o webhook do banco (Pix) ou do CTN (link) confirma o pagamento. Uma rotina dá a baixa no CTN (D31) e envia
  a confirmação fixa, com o convite para o cartão quando couber. Depois, `ENCERRADO`. Pagamento em dobro ou de valor
  diferente vai ao humano com nota e etiqueta (D39).
- **Baixa por fora** (boleto, cartão, balcão): a cada 10 minutos, com o CTN real, uma rotina lê as baixas novas, encerra
  ou abate a cobrança e manda no máximo 1 aviso fixo (D50).
- **De qualquer estado,** um sinal de saída leva a `ATENDIMENTO_HUMANO`.
- **Volta ao robô:** quando o atendente marca a conversa como Pendente.

## 4. As ações

O Luna escolhe uma **ação principal**; o código anexa o que vai junto, por uma tabela fixa (D37). O Luna nunca escolhe
`HUMAN`: quem manda ao humano são os sinais e a guarda.

| Ação | O que faz | O código anexa |
|---|---|---|
| `INFORM_DEBT` | apresenta o débito total, sem desconto (D29): a 1ª apresentação da dívida | o link do CTN se o cliente é propenso ao cartão; o Pix se não é (D36); nada, se já tem um meio vigente |
| `OFFER_DISCOUNT` | libera o próximo degrau (D35); só existe se há degrau seguinte; atende também quem diz que está sem dinheiro | o link do CTN, já com o desconto (D34) |
| `SEND_CTN_LINK` | manda o link: paga e cadastra o cartão (D30) | nada |
| `GENERATE_PIX` | gera o Pix do valor integral (D22, D34); atende também quem não quer ou não pode usar o cartão | nada |
| `ANSWER` | responde a dúvida ou o comentário sobre a cobrança com as regras da política, sem mudar o degrau nem gerar nada; sempre permitida | como a `INFORM_DEBT`: o meio de pagamento, só se não há um vigente |
| `OUT_OF_SCOPE` | o Luna redige a volta ao assunto, sem afirmar nada sobre o que foi perguntado; reprovado 2 vezes, sai a frase fixa, sem anexo nem atendente | como a `INFORM_DEBT` |
| `NONE` | sem texto: só confirmação ou agradecimento; cortesia fica em silêncio, o resto vai ao atendente (`SEM_RESPOSTA`) | nada |

Na régua sem desconto (NR, R1, R2) não há `OFFER_DISCOUNT`. Esgotados os degraus, a última oferta é o valor final.

**O meio de pagamento vigente** (`meio_vigente`, `turno/executar.py`) é a última forma enviada nesta conversa que ainda
vale: o Pix de valor igual à dívida que não vence antes de `pix_reaproveitar_minimo_minutos`, ou o link pendente sem
vencimento ou ainda não vencido. Conversa nova começa sem meio. Com um vigente, `INFORM_DEBT`, `ANSWER` e `OUT_OF_SCOPE`
não repetem o bloco (o texto aponta para o que já foi enviado); sem ele, o código anexa o Pix ou o link, e o texto leva
os marcadores. Reenviar o Pix que ainda vale não cria cobrança nova. O descarte de uma mensagem pendente esquece a forma
enviada quando a mensagem descartada a levava.

## 5. O backend calcula, o Luna redige

- O **backend** executa a ação dentro da política (`turno/executar.py`) e produz os números: a dívida, o valor com o
  degrau do desconto, o link do CTN, o BR Code do Pix e o vencimento. O Pix paga sempre o integral e o que ainda vale é
  reaproveitado. Só então os marcadores do texto são trocados.
- Um número errado não tem por onde entrar: ele é interpolado a partir do backend, e não gerado pelo Luna.
- O **recebedor do Pix** (D44) também é do backend: com `pix_recebedor` preenchido, a frase fixa `texto_pix_recebedor`
  entra no fim do texto, depois da validação.
- A mensagem é gravada como pendente e enviada ao Chatwoot por último. Se a cobrança foi paga no meio do turno, nada
  sai.
- O toque em "Pagar com Pix" (D26, D40, D59) gera ou reaproveita o Pix e responde com uma mensagem fixa, sem LLM: o Pix,
  o valor, o vencimento e o recebedor. Conta no teto.

## 6. Validação do texto pelo código

O texto do Luna é conferido contra uma prévia dos números, antes de qualquer link ou Pix de verdade: os marcadores
obrigatórios da ação estão presentes, os outros só valem se o turno tem o valor deles (os opcionais com valor, como
`{mensalidades}` e `{valor_pix}`, também são aceitos), nenhum marcador desconhecido, e não há dígito, valor ou link
solto. Reprovou: **2 tentativas**, a
2ª dizendo ao Luna o motivo. Sem texto aprovado, a conversa vai ao atendente (`VALIDACAO`), com a frase fixa, sem resposta
segura (D63); o `OUT_OF_SCOPE` é a exceção: sai a frase fixa do fora do assunto, sem anexo e sem atendente. Na `ANSWER` e
no `OUT_OF_SCOPE`, os marcadores são `{divida}` e `{mensalidades}` (opcionais); com degrau liberado, também
`{valor_final}` e `{desconto}`; com o anexo, os do Pix ou do link ficam obrigatórios; sem o anexo, `{link_ctn}` e os do
Pix são reprovados.

A **passagem** ao atendente tem o conferidor próprio (`conferir_passagem`): texto não vazio, sem número, link nem
marcador, com a palavra "atendente". Reprovada, não ganha 2ª tentativa: sai a frase fixa.

## 7. Custo e latência

Uma chamada ao Luna por turno (D59; preço na D42). O webhook do Chatwoot já chega com cerca de 1 minuto de atraso, e a
rajada espera 7 s. O tempo da linguagem de programação fica abaixo de 1% do turno (análise do D7).

## 8. O modo teste (D56, D61)

Com `[teste]` no `politica.toml`, os telefones do piloto recebem **5 casos inventados** (`cardapio-teste.toml`, matrícula
`SIM-…`) no lugar da carteira. Um roteador manda as matrículas `SIM-` ao `CTNDeCasos` e o resto ao CTN real. O Pix e o
link viram `[TESTE] …`; `/paguei` simula a confirmação do banco; `/nota` grava o retorno do testador preso ao turno
(`agente notas-teste` junta num relatório). O serviço não sobe com cobrança real ativa em telefone do piloto.

## 9. Falhas e fallback

- **O Luna, o CTN, o Pix ou o Chatwoot fora do ar:** o turno é desfeito e tentado de novo, com espera que dobra, até
  `integracao_tentativas` (3); depois, o humano (`INTEGRACAO_INDISPONIVEL`). Sem o Luna o turno não responde
  por conta própria: esgotadas as tentativas, a conversa vai ao humano.
- **Erro de código:** 3 tentativas (`erro_interno_tentativas`) e o humano (`ERRO_INTERNO`).
- **A guarda de palavra-chave** (golpe, golpista, advogado, Procon, processar) leva ao humano na hora, com a frase fixa,
  mesmo com o Luna fora do ar. O pedido de humano ("quero falar com um atendente") depende do Luna: fora do ar, o turno
  vai ao humano depois das 3 tentativas.
- **LGPD:** o histórico da conversa vai à OpenAI a cada turno. O serviço só sobe com dado real e a OpenAI juntos se
  `AGENTE_LLM_DADOS_REAIS_LIBERADOS=true`, depois da conferência da retenção de dados (T6, D42).

## 10. Integrações: interfaces com implementação falsa

Os sistemas de fora entram como interfaces (`src/agente/integracoes/`); enquanto o sistema real não chega, a
implementação é falsa. Trocar a falsa pela real não muda o resto do código.

| Interface | O que oferece | Real depende de |
|---|---|---|
| `CTN` | cliente, matrícula, histórico de pagamentos, mensalidades e baixas (**leitura real**, `CTNEficaz`); link de pagamento, link de cadastro do cartão, registro da baixa e webhook (**falsos**) | T9 (escrita) |
| `PixBanco` | cobrança Pix com txid e vencimento; webhook do Pix recebido | T1 |
| `Chatwoot` | webhook de mensagem, envio, nota privada, etiqueta, atribuição, status; download do anexo do cliente (o áudio) | T3 |
| `Disparador` | envio da abertura por template, com o botão "Pagar com Pix" (D3, D26) | T4 |
| `LLMTurno` | a chamada ao Luna, que devolve o JSON do turno (`LLMTurnoOpenAI`) | T6 |
| `Transcritor` | o áudio do cliente em texto (`TranscritorOpenAI`); a falha é um resultado, nunca uma exceção | T11 |

## Para onde ir no código

| Quero ver | Onde |
|---|---|
| a entrada, o prompt, a saída e a decisão do Luna | `src/agente/turno/uma_chamada.py` |
| o fluxo do turno, as falhas e o envio | `src/agente/turno/orquestrador.py` |
| os gates | `src/agente/turno/gates.py` |
| o handoff e as fixas | `src/agente/turno/handoff.py`, `src/agente/turno/catalogo.py` |
| quem está no piloto | `src/agente/turno/liberacao.py` |
| o webhook | `src/agente/api/chatwoot_webhook.py` |
| a política (valores, janela, textos) | `politica.toml`, `src/agente/politica.py` |
| a confirmação do pagamento e as baixas | `src/agente/pagamento/` |
| o modo teste | `src/agente/teste/`, `cardapio-teste.toml` |
