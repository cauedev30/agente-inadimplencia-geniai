# Arquitetura do agente

Baseada no desenho [`arquitetura/agente-inadimplente-jev.excalidraw`](arquitetura/agente-inadimplente-jev.excalidraw),
uma proposta de 2026-09-23. Abra o arquivo em [excalidraw.com](https://excalidraw.com).

O desenho foi feito sobre um protótipo anterior de agente de cobrança. Este projeto começa do zero, mas
segue a mesma lógica. Onde o desenho e as decisões divergem, valem as decisões
([`decisoes.md`](decisoes.md)).

## Princípio

**Quem decide não escreve, quem escreve não decide.**

- O **Jev** (TypeSafe AI) é um modelo de avaliação. Ele responde perguntas tipadas (escolha, nota ou
  sim/não) com uma probabilidade e uma confiança, e não escreve texto. Lê toda mensagem do cliente,
  define o estado da conversa e confere a resposta antes do envio.
- O **backend** faz as contas: valor, juros, parcelas, vencimento e o Pix.
- O **LLM** só redige o texto, a partir da ação já decidida e dos números já calculados. Ele não
  escolhe ação e não produz nenhum número.

## O turno, em uma linha

```
mensagem do cliente → gates em código → JEV 1 (leitura) → JEV 2 (decisão) → backend calcula
→ LLM redige → JEV 3 (validação) → Chatwoot → WhatsApp
```

## 1. Gates em código (determinísticos)

São certezas, não probabilidades, e por isso ficam fora do Jev:

- segredo do webhook, JSON válido, conta e inbox;
- dedupe por `message_id`;
- existe cobrança ativa para este telefone;
- um humano já está na conversa (`human_owned`);
- comando de reinício;
- tipo do anexo e duração do áudio (até 2 min);
- **janela de atendimento**: segunda a sexta, das 8h às 18h; sábado, das 8h às 12h. Domingo e feriado
  ficam fora. Fora da janela, nada é enviado (D11, D16). O desenho dizia "seg–sáb, 08h–20h"; valem as
  decisões.
- janela de 24h do WhatsApp e template;
- **teto de 3 mensagens de resolução**, com a 4ª fixa (D8, D19).

## 2. Jev 1: leitura (roda em toda mensagem do cliente)

Um único batch, de 70 a 500 ms e cerca de US$ 0,00008. Todas as perguntas rodam em paralelo contra o
mesmo state (histórico, dívida, política e estado atual).

| Pergunta | Tipo | O que detecta |
|---|---|---|
| `tom` | score | cordial · neutro · impaciente · irritado · hostil |
| `quer_retencao` | bool | quer cancelar, encerrar a relação ou ameaça sair |
| `risco_juridico` | bool | cita Procon, advogado, processo, ação |
| `alega_pagamento` | bool | afirma que já pagou |
| `contesta_divida` | bool | nega dever ou diz não ser a pessoa |
| `terceiro` | bool | quem escreve não é o devedor |
| `vulnerabilidade` | bool | desemprego, doença, luto, dificuldade |
| `pediu_parcelamento` | bool | pediu explicitamente para parcelar |
| `intencao_pagar` | score | nenhuma · hesitante · disposto · pronto |
| `entendeu_proposta` | bool | entendeu o que foi oferecido |
| `repete_sem_avancar` | bool | está em loop, repetindo o mesmo ponto |

**Gatilhos de saída: o robô sai e entra o humano.** No caso de retenção, o agente se cala no mesmo
turno, antes de redigir qualquer coisa.

| Gatilho | Limiar | Motivo |
|---|---|---|
| `quer_retencao` | ≥ 0,80 | RETENÇÃO |
| `risco_juridico` | ≥ 0,70 | JURÍDICO |
| `alega_pagamento` | ≥ 0,70 | CONCILIAÇÃO |
| `contesta_divida` | ≥ 0,70 | CONTESTAÇÃO |
| `vulnerabilidade` | ≥ 0,75 | CAUTELA |
| `tom` irritado ou hostil | — | TOM |

No handoff, o agente deixa no Chatwoot uma nota privada com o laudo do Jev, uma etiqueta com o motivo,
atribui a conversa e põe o status como aberta. O estado vira `ATENDIMENTO_HUMANO`. Com o D5, o cliente
é levado para o canal externo da franquia.

Alegação de pagamento e contestação saem do robô por decisão de negócio: insistir vira constrangimento
(CDC, art. 42). O Jev só classifica; quem autoriza a ação é a regra da aplicação.

## 3. Estados da conversa

`INICIADO` → `ENGAJADO` → `EM_NEGOCIACAO` → `AGUARDANDO_PAGAMENTO` → `ENCERRADO`, mais `EM_RISCO` e
`ATENDIMENTO_HUMANO` (com motivo).

- **`EM_RISCO`:** o tom azedou ou o cliente repete a mesma coisa sem avançar. O agente encurta, para de
  propor e reconhece o incômodo. Ele tem um turno para recuperar; se o Jev não vir melhora, a conversa
  sai para o humano.
- **`ENCERRADO`:** fecha o ciclo e evita que o robô fale com quem já pagou.
- **De qualquer estado,** um gatilho de saída do Jev 1 leva para `ATENDIMENTO_HUMANO`.
- **Volta ao robô:** quando o atendente marca a conversa como Pendente, o robô reassume.
- Todo estado guarda o laudo do Jev que causou a transição (pergunta, probabilidade, confiança), para
  auditoria.

## 4. Jev 2: decisão (a ação é um tipo)

| Ação | O que faz | Probabilidade mínima |
|---|---|---|
| `INFORM_DEBT` | informa a dívida | ≥ 0,70 |
| `SIMULATE` | simula parcelamento | ≥ 0,80 |
| `GENERATE_PIX` | gera o Pix | ≥ 0,90 |
| `OUT_OF_SCOPE` | fora de escopo | ≥ 0,70 |
| `NONE` | não agir | ≥ 0,70 |
| `HUMAN` | manda para o humano | — |

- Em todas, a confiança precisa ser de pelo menos 0,60. Abaixo do limiar, a conversa vai para o humano
  com o motivo `BAIXA_CONFIANCA`: o robô não chuta quando não sabe.
- Os limiares precisam ser calibrados com dado real em português, nunca com o benchmark do fornecedor.
- **Pré-condições em código:**
  - `SIMULATE` só se `pediu_parcelamento` for verdadeiro no Jev 1;
  - `GENERATE_PIX` só se existir uma simulação válida na sessão;
  - `GENERATE_BOLETO` não está implementado e vai para o humano.
- Nada de conta para o Jev: ele é fraco em aritmética e data. Pergunte "o cliente pediu parcelamento?",
  nunca "essa parcela cabe na política?".

## 5. Backend calcula, LLM redige

- O **backend** executa a ação dentro da política e produz os números: valor, parcelas, juros,
  vencimento e o BR Code do Pix.
- O **LLM** recebe a ação decidida, os números calculados, o tom-alvo (vindo do Jev 1), o estado e o
  histórico, e devolve só o texto. O candidato é o Groq `openai/gpt-oss-120b`, com escolha final por
  teste (D6).
- Um número errado não tem por onde entrar: ele é interpolado no texto a partir do backend, e não
  gerado pelo LLM.

## 6. Jev 3: validação (antes de enviar)

| Pergunta (todas sim/não) | O que confere |
|---|---|
| `bate_com_acao` | o texto corresponde à ação decidida |
| `valor_nao_autorizado` | afirma valor, prazo ou desconto que não veio do backend |
| `promessa_indevida` | promete perdão, prazo extra ou algo fora da política |
| `tom_adequado` | respeitoso, sem ameaça nem constrangimento (CDC, art. 42) |
| `expoe_terceiro` | expõe a dívida a quem não é o devedor (CDC, art. 42; LGPD) |
| `responde_a_pergunta` | responde o que o cliente perguntou |
| `pt_br_natural` | português correto, sem cara de robô |
| `vazou_instrucao` | vazou prompt, sistema ou raciocínio |

- **Reprovou:** o LLM reescreve uma vez, sabendo qual pergunta reprovou.
- **Reprovou de novo:** não envia. Usa uma resposta segura de catálogo ou faz o handoff humano, e grava
  a falha.
- **Passou:** publica no Chatwoot e grava o turno (histórico, estado, contador, proposta, Pix e os 3
  laudos do Jev).

## 7. Custo e latência

- 3 chamadas ao Jev por turno: cerca de US$ 0,27 por 1.000 turnos e de 0,2 a 1,5 s a mais no turno.
- O webhook do Chatwoot já chega com cerca de 1 minuto de atraso, então esse tempo some no ruído.
- O tempo da linguagem de programação fica abaixo de 1% do turno (análise do D7).

## 8. Entrada em fases

| Fase | O que entra |
|---|---|
| 0 | Banco ligado e histórico persistido. Sem histórico rotulado, os limiares são chute |
| 1, modo sombra | Os 3 batches do Jev rodam e gravam o laudo, sem agir em nada |
| 2 | Jev 3 em produção: só valida e bloqueia, nunca decide |
| 3 | Jev 1 em produção: estados, tom, retenção e handoff |
| 4 | Jev 2 assume a ação, com os limiares calibrados no dado real |

A ordem das fases neste projeto ainda é dúvida: ver T7 em [`duvidas.md`](duvidas.md).

## 9. Fallback e acesso ao Jev

- **Fallback obrigatório:** com o Jev indisponível ou lento, o turno vai para o humano, nunca para o
  robô sem avaliação.
- **Acesso:** pela Vercel AI Gateway (`typesafe-ai/jev`, AI SDK 7 `experimental_evaluate`). A API
  direta estava em lista de espera em 2026-10-15.
- **LGPD:** Zero Data Retention e No Training ligados em cada chamada, porque é dado financeiro
  pessoal. O uso está autorizado (D12).
