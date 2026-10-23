# Agente de Inadimplência — GeniAI

Agente de IA que cobra, pelo WhatsApp, os clientes inadimplentes das franquias do Cartão de Todos que
usam o disparador da GeniAI. O objetivo é o cliente pagar com o menor número de mensagens possível. O
Jev decide o que fazer, o backend calcula os valores e o Pix, e o LLM só escreve o texto.

A primeira franquia é o **Cartão de Todos João Pessoa Sul**.

## Status

**Em implementação: etapas 1 a 5 prontas, com o serviço em modo sombra.** A spec está fechada para começar ([`docs/spec.md`](docs/spec.md)).
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
| [`docs/integracoes.md`](docs/integracoes.md) | Guia para ligar os sistemas reais (T1 a T11): o que precisamos, onde entra e o teste de cada um |

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
uv run agente comparar-redatores tests/conversas --modelo openai/gpt-oss-120b  # compara os LLMs (T6); exige AGENTE_GROQ_API_KEY
```

## Deploy

O serviço roda em modo sombra até o Chatwoot (T3), o Jev real (T10) e o servidor (T2) estarem
definidos: lê, grava os laudos do Jev e o que o turno teria feito, sem enviar nada. Até a T10, o
modo ativo é recusado. Com o `AGENTE_MODO=ativo`, a `liberacao` do `politica.toml` decide quais
conversas saem da sombra: o piloto com os telefones da equipe, uma parte da carteira ou a carteira toda.

```bash
docker build -t agente-inadimplencia .
docker run -p 8000:8000   -e AGENTE_DATABASE_URL=postgresql+psycopg://usuario:senha@host/agente   -e AGENTE_WEBHOOK_SEGREDO=...   agente-inadimplencia
```

O contêiner roda `alembic upgrade head` e depois o uvicorn. Sem Docker:
`uv run uvicorn agente.api.principal:montar_app --factory`.

| Endpoint | Para quê |
|---|---|
| `GET /saude` | o serviço está vivo, sem tocar no banco (o health check consulta esta) |
| `GET /saude/webchat` | quantas amarrações do webchat esgotaram as tentativas e seguem pendentes; 503 se o banco falhar |
| `GET /saude/pagamentos` | quantos pagamentos pagos não concluem (baixa ou confirmação); 503 se o banco falhar |
| `POST /webhooks/chatwoot/{segredo}` | webhook do Agent Bot no Chatwoot (mensagens, status e a conversa nova do webchat) |
| `GET` e `POST /atendimento/{token}` | a página do webchat: pede os primeiros dígitos do CPF e abre o widget (503 sem os segredos do webchat) |

As variáveis estão no [`.env.example`](.env.example).

## Configuração do Chatwoot

O que o serviço espera da conta do Chatwoot (T3). A conta, as inboxes e o time são criados na
interface do Chatwoot; o serviço só lê os ids das variáveis de ambiente.

**Inbox do WhatsApp (o robô):**

- o Agent Bot da inbox aponta para `POST /webhooks/chatwoot/{AGENTE_WEBHOOK_SEGREDO}`, com os
  eventos `message_created` e `conversation_status_changed`;
- `AGENTE_CHATWOOT_URL`, `AGENTE_CHATWOOT_CONTA`, `AGENTE_CHATWOOT_INBOX` e `AGENTE_CHATWOOT_TOKEN`
  (o token do bot); `AGENTE_CHATWOOT_TIME_HUMANO` é o time que recebe os handoffs.

**Inbox Website (o webchat, D32):**

1. Crie uma inbox do tipo **Website**. O atendente responde por ela, no Chatwoot; o cliente fala pelo
   widget na página `/atendimento/{token}`.
2. **Rodízio:** nas configurações da inbox, em *Collaborators*, marque os atendentes do plantão e
   ligue **Enable auto assignment**. O Chatwoot reparte as conversas novas entre eles, em rodízio
   igualitário. É configuração do Chatwoot, o código do agente não escolhe o atendente. O plantão
   (quem está online na janela de atendimento, D16) também é gerido ali (G7).
3. Em *Settings > Configuration*, ligue a **validação de identidade** e marque **Enforce User Identity
   Validation** (obrigar). Sem o obrigatório, o widget aceita qualquer `identifier` sem hash, e o
   `cobranca-<id>` é fácil de adivinhar. Copie o **HMAC token** da inbox para
   `AGENTE_WEBCHAT_IDENTIDADE_HMAC`. Além da configuração, o código só amarra a conversa da cobrança
   que acertou o CPF há menos de `webchat_verificacao_validade_minutos`. O servidor calcula o
   `identifier_hash` com ele; a chave nunca vai ao navegador.
4. Copie o **token do site** (*website token*, o do script do widget) para
   `AGENTE_WEBCHAT_WEBSITE_TOKEN` e o id da inbox para `AGENTE_WEBCHAT_INBOX`.
5. Aponte um webhook da conta para `POST /webhooks/chatwoot/{AGENTE_WEBHOOK_SEGREDO}` com os eventos
   `conversation_created` e `message_created`: eles amarram a conversa do webchat à cobrança (nota com o resumo,
   etiqueta, time e a conversa do WhatsApp marcada como transferida).
6. `AGENTE_WEBCHAT_SEGREDO` assina o token do link (gere um valor longo e aleatório) e
   `AGENTE_WEBCHAT_URL_BASE` é o domínio público da página (G7). Sem `AGENTE_WEBCHAT_URL_BASE`, a 6ª
   mensagem mantém o link provisório do `politica.toml`; sem o segredo, a página `/atendimento` fica
   desligada (503).

Os parâmetros de negócio do webchat (validade do link, dígitos do CPF e tentativas) estão no
`politica.toml`, com o prefixo `webchat_`. Depois dos erros no CPF o link bloqueia por `webchat_cpf_bloqueio_horas`; a conversa do WhatsApp ganha a etiqueta `webchat_bloqueado` e uma nota privada, uma vez por bloqueio.
