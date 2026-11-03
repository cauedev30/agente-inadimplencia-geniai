# Calibração do Jev real (TypeSafe) nas conversas simuladas

Data: 2026-11-03. Modelo: `typesafe:jev-1.14.0`. Dados: as 16 conversas de `tests/conversas` (18 turnos),
**todas inventadas**: este relatório pode ir ao git. Nada de conversa real foi ao Jev (D12).
Comando: `agente simular tests/conversas --jev-real`. Os limiares estão em `politica.toml`
(`[limiares_jev1]`, `[limiares_jev2]`, `confianca_minima = 0.60`) e **ainda são os palpites iniciais**.

## Resumo

- **Rodada 1 (CLI):** 8 divergências em 18 turnos. **Rodada 2 (com os laudos gravados):** 9. O Jev **não é
  determinístico**: a diferença é um turno que ficou em 0,77 contra o limiar de 0,80. Qualquer ajuste precisa
  de margem, e a conclusão por pergunta vale com a ressalva de que são 1 a 6 amostras cada.
- **O erro grande não é o julgamento, é a decisão da ação (Jev 2):** o `acao` leva quase toda objeção de valor a
  `HUMAN:BAIXA_CONFIANCA`, porque a probabilidade fica abaixo do limiar de `OFFER_DISCOUNT` (0,80) ou a confiança
  abaixo de 0,60. O Jev 1 viu a objeção com 0,70 a 0,94 em todos os casos.
- Os **gatilhos de saída para o humano acertaram**: retenção, jurídico, conciliação, golpe e energia, todos com
  folga grande sobre o limiar. Nenhum gatilho disparou sem dever (zero falso positivo nos 12 turnos sem gatilho).
- O `tom` erra (2 de 5), em parte por gabarito discutível.

## Acertos por pergunta (rodada 1)

| Pergunta | Acertos |
|---|---|
| `acao` | 10/12 (83%) |
| `intencao_pagar` | 5/6 (83%) (rodada 2: 4/6) |
| `tom` | 2/5 (40%) (rodada 2: 2/5) |
| `objecao_valor` | 6/6 |
| `alega_pagamento`, `atrito_clinica`, `desconfia_golpe`, `quer_conta_energia`, `quer_retencao`, `risco_juridico` | 1/1 cada |

Por ação: `OFFER_DISCOUNT` 0/3, `HUMAN:ATRITO` 0/1, `HUMAN:OBJECAO_SEM_DESCONTO` 0/1, `INFORM_DEBT` 4/5; as
outras acertaram. O contador da `acao` conta o laudo contra o gabarito; as divergências abaixo contam o
resultado do turno (depois dos limiares).

## Jev 2: a probabilidade da `acao` contra o limiar (rodada 2)

| Turno (cliente) | Gabarito | Jev 2 escolheu | p | confiança | Limiar da ação | Resultado |
|---|---|---|---|---|---|---|
| "Tá caro, tem desconto?" (NR) | `OFFER_DISCOUNT` | `OFFER_DISCOUNT` | 0,81 | 0,78 | 0,80 | passa (por 0,01) |
| "Sem desconto eu não pago." (NR) | `OFFER_DISCOUNT` | `NONE` | 0,57 | 0,50 | 0,70 | `BAIXA_CONFIANCA` |
| "Tá caro." (R3) | `OFFER_DISCOUNT` | `OFFER_DISCOUNT` | 0,47 | 0,38 | 0,80 | `BAIXA_CONFIANCA` |
| "Não consigo pagar tudo de uma vez." (Prévia) | `OFFER_DISCOUNT` | **`INFORM_DEBT`** | 0,35 | 0,24 | 0,70 | `BAIXA_CONFIANCA` |
| "Quero resolver isso hoje, como eu pago?" (R1) | `INFORM_DEBT` | **`GENERATE_PIX`** | 0,68 | 0,63 | 0,90 | `BAIXA_CONFIANCA` |
| "Tá muito caro, não tem um desconto?" (R2) | `OFFER_DISCOUNT` | `OFFER_DISCOUNT` | 0,77 | 0,73 | 0,80 | `BAIXA_CONFIANCA` (rodada 1: passou) |
| "Consegue fazer um valor melhor?" (R3, 2º) | `OFFER_DISCOUNT` | `OFFER_DISCOUNT` | 0,48 | 0,39 | 0,80 | `BAIXA_CONFIANCA` |
| "Me manda o link ... no cartão." (R4) | `SEND_CTN_LINK` | `SEND_CTN_LINK` | 0,93 | 0,92 | 0,85 | passa |
| "Qual o horário ... da clínica?" (R5) | `OUT_OF_SCOPE` | `OUT_OF_SCOPE` | 0,75 | 0,71 | 0,70 | passa (por 0,05) |
| "Ok, vi aqui." (R6) | `NONE` | `NONE` | 0,70 | 0,65 | 0,70 | passa (por 0,00) |
| "Oi ... Quanto está o meu débito?", "Bom dia, quanto eu estou devendo?" | `INFORM_DEBT` | `INFORM_DEBT` | 0,98 | 0,98 | 0,70 | passa |

Leitura: o Jev escolheu a ação certa em 4 dos 6 turnos de desconto, mas **com probabilidade baixa e instável**.
Duas escolhas estão erradas (`INFORM_DEBT` na Prévia; `GENERATE_PIX` já no 1º turno do R1, em que o gabarito
manda apresentar o débito antes). `INFORM_DEBT` e `SEND_CTN_LINK` ficam altos (0,93 a 0,98).

## Jev 1: gatilhos contra o limiar (rodada 2)

| Gatilho | p | Limiar | Folga |
|---|---|---|---|
| `quer_retencao` | 0,96 | 0,80 | +0,16 |
| `risco_juridico` | 0,93 | 0,70 | +0,23 |
| `alega_pagamento` | 0,84 | 0,70 | +0,14 |
| `desconfia_golpe` | 0,97 | 0,70 | +0,27 |
| `atrito_clinica` | 0,95 | 0,70 | +0,25 |
| `quer_conta_energia` | 0,94 | 0,70 | +0,24 |

- **Atrito com a clínica** saiu como `CONTESTACAO`: o Jev marcou `contesta_divida` 0,86 junto de
  `atrito_clinica` 0,95, e a ordem de `GATILHOS` (`jev1.py:44`) dá a vez à contestação. É empate de prioridade,
  não erro de leitura; o destino é o mesmo (humano), muda só o rótulo.
- `contesta_divida` disparou também em "já paguei" (0,75, limiar 0,70) e no Procon (0,65, abaixo do limiar):
  gatilhos que se sobrepõem, sem efeito porque a conciliação e o jurídico vêm antes.
- `objecao_valor` foi lida em todos os 6 turnos de desconto, de 0,70 a 0,94 (o mais baixo: "Tá caro." do R3).
- `repete_sem_avancar` marcou 0,63 em "Sem desconto eu não pago." (2º turno do NR), o que é razoável.

## `tom` e `intencao_pagar`

- `tom`: NR neutro → cordial; R1 cordial → **impaciente** (0,81); atrito impaciente → irritado (0,88);
  R3 cordial e R6 neutro acertaram. "Quero resolver isso hoje, como eu pago?" ser `impaciente` é discutível, e o
  gabarito (`cordial`) também. As probabilidades do `tom` ficam baixas (0,53 a 0,76) nas frases curtas.
  Atenção: `impaciente` deixa a conversa `EM_RISCO`, e `irritado`/`hostil` mandam para o humano.
- `intencao_pagar`: errou "Oi, ... Quanto está o meu débito?" (hesitante → nenhuma, p 0,42, conf 0,11) e, na
  rodada 2, a Prévia (hesitante → disposto, 0,53). A confiança chega a 0,11 e a 0,0 em alguns laudos.

## Latência e custo

- **Latência:** a simulação inteira (40 chamadas ao Jev) levou ~11 s, cerca de **0,27 s por chamada**, bem
  abaixo do `jev_timeout_ms` (2000 ms; o cliente usa 1,5 s).
- **Custo:** 32.678 tokens de entrada na rodada 1 e 30.990 na 2, para 18 turnos: ~1.800 por turno, ~800 por
  chamada (a saída é grátis). **Preço por token: a confirmar** (não está na documentação que usamos).

## Hipótese e próximo passo (não aplicados)

O cliente manda ao Jev 2 as opções do `choice` como `criteria = {opção: opção}`, isto é, **só os códigos**
(`INFORM_DEBT`, `OFFER_DISCOUNT`...), e a explicação de cada ação está na `descricao` da pergunta, em prosa e
em outra ordem. A documentação da TypeSafe pede `criteria` como nome da escolha → descrição. A hipótese é que as
probabilidades baixas e instáveis da `acao` venham de o modelo ter que ligar a prosa aos códigos. **Não
testada.** Propostas a decidir com o Cauê (nenhuma aplicada):

1. Dar a cada opção da `acao` uma descrição própria nos `criteria` (cada ação com uma linha), e deixar a
   `descricao` da pergunta curta.
2. Reescrever a `descricao` da `acao` com o critério do 1º turno ("apresente o débito antes do Pix") e do
   desconto ("o cliente acha caro, não consegue pagar tudo ou pede desconto").
3. Só depois, se a probabilidade continuar abaixo de 0,80, rever os limiares: 0,80 para `OFFER_DISCOUNT` e 0,90
   para `GENERATE_PIX` foram palpites. Rever limiar com 18 turnos e um Jev que varia é arriscado; a decisão
   precisa de mais conversas simuladas, escritas com frases mais naturais.
4. Revisar os gabaritos de `tom` com o Cauê (cordial, neutro e impaciente em frases curtas).

Conforme o plano (11.3), roda-se **uma vez** de novo depois da `descricao` aprovada.
