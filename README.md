# Agente de Inadimplência — GeniAI

Agente de IA que cobra, pelo WhatsApp, os clientes inadimplentes das franquias do Cartão de Todos que
usam o disparador da GeniAI. O objetivo é o cliente pagar com o menor número de mensagens possível. Uma
chamada ao Luna (`gpt-6-luna`) lê a conversa, escolhe a ação e redige o texto; o código confere a ação, os
valores, os gatilhos de humano e o teto, e o backend calcula os valores e gera o Pix.

A primeira franquia é o **Cartão de Todos João Pessoa Sul**.

## Status

**Em implementação, no corte de 2026-11-11 (D59, D60, D61).** O código está escrito e os testes automáticos
passam, mas o serviço ainda não está hospedado e ninguém o testou à mão. Só conversam os telefones do piloto.
O Pix, o link e a baixa do CTN e o disparador são peças falsas até os sistemas reais chegarem (T1, T9 e T4,
em [`docs/integracoes.md`](docs/integracoes.md)); a leitura do CTN é real, mas fica parada até o piloto real. Só o
Luna foi conferido com o modelo real, em ensaio com um Chatwoot falso. A linha de chegada atual
(o teste do Luciano e da Lohanny no `dev2`) está na seção 6 de [`docs/spec.md`](docs/spec.md). O que ficou de
fora dela está em [`docs/depois.md`](docs/depois.md).

## Documentação

| Arquivo | O que tem |
|---|---|
| [`docs/spec.md`](docs/spec.md) | O produto: o problema, o escopo, as regras da conversa e o critério de aceite |
| [`docs/arquitetura.md`](docs/arquitetura.md) | Como um turno funciona: webhook, gates, uma chamada ao Luna, validação pelo código, envio e handoff |
| [`docs/decisoes.md`](docs/decisoes.md) | Decisões numeradas (D1, D2, …), com data |
| [`docs/duvidas.md`](docs/duvidas.md) | Perguntas em aberto, separadas por quem responde, cada uma com o padrão usado até a resposta |
| [`docs/depois.md`](docs/depois.md) | O que ficou fora da linha de chegada: os achados da auditoria e as pendências |
| [`docs/comparacao-llm.md`](docs/comparacao-llm.md) | Por que o LLM é o `gpt-6-luna`: as duas rodadas de comparação, com os resultados brutos (histórico) |
| [`docs/ideia-waha.md`](docs/ideia-waha.md) | Ideia do humano num número WAHA; sem efeito pela D60 (histórico) |
| [`docs/integracoes.md`](docs/integracoes.md) | Guia para ligar os sistemas reais (T1 a T11): o que precisamos, onde entra e o teste de cada um |

Saíram do código e ficam na tag `antes-do-enxugamento`: o pipeline do Jev, a simulação e a comparação de
redatores.
Sem efeito pela D59, D60 e D61: o modo sombra e o webchat.

## Stack

Python 3.12 (D7), com [uv](https://docs.astral.sh/uv/), FastAPI, SQLAlchemy 2, Alembic, pydantic, pytest e
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

Comandos do agente (`agente --help`), com `AGENTE_DATABASE_URL` definido e o banco migrado por
`uv run alembic upgrade head`:

```bash
uv run agente importar carteira.csv --campanha outubro   # importa a carteira de um CSV
uv run agente disparar --campanha outubro                 # dispara as aberturas (disparador falso até a T4)
uv run agente importar-ctn [--data AAAA-MM-DD] [--meses-minimos N] [--simular]   # a carteira do dia, pelo retrato do CTN (AGENTE_CTN_API_KEY)
uv run agente notas-teste [--desde AAAA-MM-DD] [--saida arquivo.md]   # as notas (/nota) dos testadores, em Markdown
```

O `disparar` só abre conversa com os telefones do piloto. O `importar-ctn` só lê o CTN (`GET`) e a saída traz
só contagens; as opções e os códigos de saída estão em [`docs/integracoes.md`](docs/integracoes.md).

## Deploy

O serviço exige o Chatwoot real (T3) e a chave do Luna: sem eles não sobe. Só conversa quem está na
`liberacao` do `politica.toml` (o piloto, com os telefones da lista, D61); quem está fora é ignorado,
sem Luna e sem mensagem, e a lista vazia (o padrão) não conversa com ninguém. Como o serviço sempre tem o
Chatwoot real e a chave do Luna, ele só sobe com `AGENTE_LLM_DADOS_REAIS_LIBERADOS=true`, que espera a
conferência da retenção de dados da OpenAI (T6, D42).

```bash
docker build -t agente-inadimplencia .
docker run -p 8000:8000 --env-file .env agente-inadimplencia   # AGENTE_DATABASE_URL=postgresql+psycopg://usuario:senha@host/agente
```

O contêiner roda `alembic upgrade head` e depois o uvicorn. Sem Docker:
`uv run uvicorn agente.api.principal:montar_app --factory`.

| Endpoint | Para quê |
|---|---|
| `GET /saude` | o serviço está vivo, sem tocar no banco (o health check consulta esta) |
| `GET /saude/pagamentos` | quantos pagamentos pagos não concluem (baixa ou confirmação); 503 se o banco falhar |
| `POST /webhooks/chatwoot/{segredo}` | webhook do Agent Bot no Chatwoot (mensagens e mudança de status) |
| `POST /webhooks/pix` e `POST /webhooks/ctn` | confirmação de pagamento do banco e do link do CTN, assinada por HMAC; 503 sem o segredo |

As variáveis estão no [`.env.example`](.env.example).

## LLM e transcrição do áudio (T6, T11)

O Luna é o `gpt-6-luna` da OpenAI (D42) e o áudio do cliente é transcrito pelo `gpt-4o-mini-transcribe`
(D43). O serviço só sobe com a chave do Luna. Sem a transcrição, o áudio cai no pedido de texto.

- `AGENTE_LLM_API_KEY` liga o Luna (obrigatória). `AGENTE_LLM_URL`, `AGENTE_LLM_MODELO` e
  `AGENTE_LLM_CORPO_EXTRA` já vêm com os padrões do Luna. O Luna recusa a temperatura sem o
  `reasoning_effort: "none"`, que o corpo extra padrão manda;
- `AGENTE_TRANSCRICAO_URL` e `AGENTE_TRANSCRICAO_API_KEY` ligam a transcrição, e
  `AGENTE_TRANSCRICAO_MODELO` troca o modelo. Com só uma das duas, o serviço não sobe e diz qual falta;
- `AGENTE_LLM_DADOS_REAIS_LIBERADOS` (padrão `false`) trava a subida com dado real de cliente indo à
  OpenAI (M2);
- os limites do áudio e os textos do pedido de texto estão no `politica.toml`.

O passo a passo está em [`docs/integracoes.md`](docs/integracoes.md) (T6 e T11).

## Configuração do Chatwoot

O que o serviço espera da conta do Chatwoot (T3). A conta, a inbox e o time são criados na interface do
Chatwoot; o serviço só lê os ids das variáveis de ambiente.

- o Agent Bot da inbox do WhatsApp aponta para `POST /webhooks/chatwoot/{AGENTE_WEBHOOK_SEGREDO}`, com os
  eventos `message_created` e `conversation_status_changed`;
- `AGENTE_CHATWOOT_URL`, `AGENTE_CHATWOOT_CONTA`, `AGENTE_CHATWOOT_INBOX` e `AGENTE_CHATWOOT_TOKEN`
  (o token do bot); `AGENTE_CHATWOOT_TIME_HUMANO` é o time que recebe os handoffs;
- **o atendente responde na mesma conversa do WhatsApp** (D60). Não há segundo canal nem link. O rodízio
  entre os atendentes é a atribuição automática do Chatwoot (nas configurações da inbox, *Collaborators* e
  *Enable auto assignment*), que o código não escolhe (*a confirmar* na conta do JP Sul);
- quando o atendente marca a conversa como **Pendente**, o robô reassume.
