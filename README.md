# Agente de Inadimplência — GeniAI

Agente de IA que cobra, pelo WhatsApp, os clientes inadimplentes das franquias do Cartão de Todos que
usam o disparador da GeniAI. O objetivo é o cliente pagar com o menor número de mensagens possível. O
Jev decide o que fazer, o backend calcula os valores e o Pix, e o LLM só escreve o texto.

A primeira franquia é o **Cartão de Todos João Pessoa Sul**.

## Status

**Em especificação.** Ainda não há código. As decisões já tomadas estão em
[`docs/decisoes.md`](docs/decisoes.md). As perguntas que faltam responder para fechar a spec estão em
[`docs/duvidas.md`](docs/duvidas.md).

## Documentação

| Arquivo | O que tem |
|---|---|
| [`docs/spec.md`](docs/spec.md) | O produto: o problema, o escopo e as regras da conversa |
| [`docs/arquitetura.md`](docs/arquitetura.md) | Como um turno funciona: gates, Jev 1, 2 e 3, estados, ações e entrada em fases |
| [`docs/arquitetura/agente-inadimplente-jev.excalidraw`](docs/arquitetura/agente-inadimplente-jev.excalidraw) | O desenho da arquitetura (abra em [excalidraw.com](https://excalidraw.com)) |
| ⭐ [`docs/ideia-webchat.md`](docs/ideia-webchat.md) | **Ideia em avaliação, com prioridade alta:** atendimento humano por webchat no lugar de outro WhatsApp |
| [`docs/decisoes.md`](docs/decisoes.md) | Decisões numeradas (D1, D2, …), com data |
| [`docs/duvidas.md`](docs/duvidas.md) | Perguntas em aberto, separadas por quem responde |

## Stack

Python (decisão D7). O código vai entrar neste repositório depois que a spec fechar e o plano de
implementação for aprovado.
