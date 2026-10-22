# Agente de Inadimplência — GeniAI

Agente de IA que cobra, pelo WhatsApp, os clientes inadimplentes das franquias do Cartão de Todos que
usam o disparador da GeniAI. O objetivo é o cliente pagar com o menor número de mensagens possível. O
Jev decide o que fazer, o backend calcula os valores e o Pix, e o LLM só escreve o texto.

A primeira franquia é o **Cartão de Todos João Pessoa Sul**.

## Status

**Em implementação: etapas 1 a 4 prontas, com o serviço em modo sombra.** A spec está fechada para começar ([`docs/spec.md`](docs/spec.md)).
As decisões estão em [`docs/decisoes.md`](docs/decisoes.md), e as perguntas em aberto, cada uma com o
padrão usado até a resposta, em [`docs/duvidas.md`](docs/duvidas.md).

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

Python 3.12 (D7), com [uv](https://docs.astral.sh/uv/), SQLAlchemy 2, Alembic, pydantic, pytest e
ruff.

## Como rodar

```bash
uv sync                      # instala as dependências no .venv
cp .env.example .env         # e preencha
uv run pytest                # testes
uv run ruff check .          # lint
uv run ruff format --check . # formatação
```

No Windows sem o `uv` no PATH, use `python -m uv` no lugar de `uv`.

Comandos do agente (com `AGENTE_DATABASE_URL` definido e o banco migrado por `uv run alembic upgrade head`):

```bash
uv run agente importar carteira.csv --campanha outubro   # importa a carteira
uv run agente disparar --campanha outubro                 # dispara as aberturas (disparador falso até a T4)
uv run agente simular tests/conversas                     # roda as conversas simuladas (Jev falso até a T10)
```

## Deploy

O serviço roda em modo sombra até o Chatwoot (T3), o Jev real (T10) e o servidor (T2) estarem
definidos: lê, grava os laudos do Jev e o que o turno teria feito, sem enviar nada. Até a T10, o
modo ativo é recusado.

```bash
docker build -t agente-inadimplencia .
docker run -p 8000:8000   -e AGENTE_DATABASE_URL=postgresql+psycopg://usuario:senha@host/agente   -e AGENTE_WEBHOOK_SEGREDO=...   agente-inadimplencia
```

O contêiner roda `alembic upgrade head` e depois o uvicorn. Sem Docker:
`uv run uvicorn agente.api.principal:montar_app --factory`.

| Endpoint | Para quê |
|---|---|
| `GET /saude` | verificação de saúde |
| `POST /webhooks/chatwoot/{segredo}` | webhook do Agent Bot no Chatwoot |

As variáveis estão no [`.env.example`](.env.example).
