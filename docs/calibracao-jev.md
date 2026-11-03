# Calibração do Jev real (TypeSafe) nas conversas simuladas

Data: 2026-11-03. Modelo: `typesafe:jev-1.14.0`. Dados: as 16 conversas de `tests/conversas` (18 turnos),
**todas inventadas**: este relatório pode ir ao git. Nada de conversa real foi ao Jev (D12).
Comando: `agente simular tests/conversas --jev-real`. Os limiares estão em `politica.toml`
(`[limiares_jev1]`, `[limiares_jev2]`, `confianca_minima = 0.60`) e **ainda são os palpites iniciais**.

> **Atualização (rodada 2, abaixo):** com a descrição por opção, as divergências caíram de 8–9 para 2 em duas
> execuções iguais, e a `acao` passou a ter probabilidade de 0,93 a 1,0. Os limiares **não foram mexidos**.

## Resumo da rodada 1

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

## Rodada 2: descrição por opção (2026-11-03)

**O que mudou** (commit `ebc78ab`, uma vez só, como manda o plano): cada opção do `choice` (`acao`) e cada nível
do `score` (`tom`, `intencao_pagar`) passou a ir com a sua descrição em `criteria` (antes, só o código); a
`descricao` da `acao` ficou curta; a de `contesta_divida` ganhou "recusar pagar ou reclamar da clínica não é
contestar"; os gabaritos de `tom` do R1 (`cordial` → `neutro`) e do atrito (`impaciente` → `irritado`) foram
trocados com o Cauê. Os textos estão em `turno/jev2.py` (`CRITERIOS_DA_ACAO`) e `turno/jev1.py`. Limiares,
`politica.toml` e o código de decisão **não mudaram**. Duas execuções, laudos gravados.

| | Rodada 1 | Rodada 2 (a) | Rodada 2 (b) |
|---|---|---|---|
| Turnos que rodaram | 18 | 20 | 20 |
| Divergências | 8 (e 9) | **2** | **2** (as mesmas) |
| `acao` | 10/12 (83%) | **13/14 (93%)** | 13/14 |
| `intencao_pagar` | 5/6 | **7/7** | 7/7 |
| `tom` | 2/5 | **4/5** | 4/5 |
| `objecao_valor` | 6/6 | 7/7 | 7/7 |
| `OFFER_DISCOUNT` | 0/3 | **4/4** | 4/4 |
| Gatilhos de saída | todos | todos | todos |
| Tokens de entrada | 32.678 / 30.990 | 48.203 | 48.223 |

- **A `acao` do Jev 2 contra o limiar:** `OFFER_DISCOUNT` 0,93 a 1,00 (limiar 0,80; antes 0,47 a 0,81), `INFORM_DEBT`
  0,98 a 1,00 (0,70), `GENERATE_PIX` 1,00 (0,90), `SEND_CTN_LINK` 1,00 (0,85), `OUT_OF_SCOPE` 1,00 (0,70), `NONE`
  0,98 a 0,99 (0,70). A menor folga é +0,13. As confianças vão de 0,92 a 1,00. **Nenhum limiar precisa mudar.**
- O R1 no 1º turno ("Quero resolver isso hoje, como eu pago?") agora recebe `INFORM_DEBT`, e o Pix só vem no 2º.
  A Prévia ("Não consigo pagar tudo de uma vez.") recebe `OFFER_DISCOUNT` (0,93).
- O atrito com a clínica sai como `ATRITO`: o `contesta_divida` deixou de marcar nele.
- As duas execuções deram as mesmas decisões, com probabilidades a ±0,05. Na rodada 1 havia um turno que mudava
  de lado entre execuções; aqui não.
- **Custo:** ~850 tokens de entrada por chamada (antes ~775, +9%), porque a descrição vai em cada pedido. Por
  turno o gasto subiu mais (~2.400 contra ~1.700) porque os casos agora andam mais: 20 turnos em vez de 18.

**As 2 divergências que sobraram** (iguais nas duas execuções, as duas vão ao humano):
1. *"Sem desconto eu não pago." (NR, 2º turno):* esperado `HUMAN:OBJECAO_SEM_DESCONTO`, saiu `HUMAN:TOM`. O Jev leu
   `tom = irritado` com 0,62 (e `repete_sem_avancar` 0,62), e o código manda ao humano por tom `irritado` ou
   `hostil` **sem olhar a probabilidade** (`jev1.py:129`). Muda só o motivo gravado para o atendente. Pode ser a
   descrição de `irritado` ("recusa de forma ríspida") puxando a frase. Decisão do Cauê: aceitar o motivo `TOM`
   aí, ou exigir um limiar de probabilidade para o tom de saída.
2. *R3, "objeção 3 vezes":* a conversa foi ao humano depois do 2º turno (esperado seguir até o 3º). O Jev leu
   `impaciente` (0,54 a 0,57) e `repete_sem_avancar = true` (0,89) em "Ainda tá caro para mim.". A regra "EM_RISCO
   tem um turno para recuperar" (`turno/avaliacao.py:104`) é de projeto. O caminho exato até o humano *a confirmar*.
   O gabarito supôs que o cliente que repete a objeção segue recebendo degraus; o Jev real disse que ele está em
   loop. Pode ser o comportamento certo.

**`tom`: o erro que sobrou** é o R1, 1º turno: o gabarito (agora `neutro`) contra a leitura `impaciente` (0,82 a
0,83) nas duas execuções. Não deixa a conversa em risco porque a ação foi `INFORM_DEBT`, mas põe `EM_RISCO` no
estado. É o "hoje" que pesa? Decisão do Cauê (gabarito ou descrição de `impaciente`).

**O que esta rodada NÃO prova**
- Foram **4 mudanças juntas** (descrição por opção, `descricao` da `acao`, `contesta_divida`, gabaritos). Não sei
  qual pesou mais; só que o conjunto resolveu. A causa original (opções só com o código) segue como a explicação
  mais provável, apoiada na documentação, mas não foi testada isolada.
- São **20 turnos inventados**, 1 a 7 por pergunta, escritos por quem conhece as descrições. Texto real do
  WhatsApp (gírias, áudio transcrito, mensagens longas, vários assuntos) não foi visto; para isso é a 11.4.
- Os gabaritos mudaram na mesma rodada que as descrições: 2 dos 5 de `tom` foram ajustados depois de ver a
  leitura. Com os gabaritos antigos o `tom` seria 3/5.
- Latência não foi medida nesta rodada (rodada 1: ~0,27 s por chamada).

## Hipótese da rodada 1 (aplicada na rodada 2)

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

Os itens 1, 2 e 4 foram aplicados na rodada 2; o 3 (limiares) **não foi necessário**.
