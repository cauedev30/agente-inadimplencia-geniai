# Agente de Inadimplência — GeniAI

Agente de IA que cobra, pelo WhatsApp, os clientes inadimplentes das franquias do Cartão de Todos que
usam o disparador da GeniAI. O objetivo é o cliente pagar com o menor número de mensagens possível. O
Jev decide o que fazer, o backend calcula os valores e o Pix, e o LLM só escreve o texto.

A primeira franquia é o **Cartão de Todos João Pessoa Sul**.

## Status

**Em implementação. O código da v1 (etapas 1 a 7) e o da etapa 8 (redator e áudio) estão escritos e os
testes automáticos passam, mas o serviço ainda não está hospedado em lugar nenhum e ninguém o testou à
mão.** O Jev, o Pix, o CTN e a transcrição são peças falsas até os sistemas reais serem ligados
([`docs/integracoes.md`](docs/integracoes.md)); só o redator `gpt-6-luna` foi conferido com o modelo
real. A spec está fechada ([`docs/spec.md`](docs/spec.md)).
As decisões estão em [`docs/decisoes.md`](docs/decisoes.md), e as perguntas em aberto, cada uma com o
padrão usado até a resposta, em [`docs/duvidas.md`](docs/duvidas.md).

## Documentação

| Arquivo | O que tem |
|---|---|
| [`docs/spec.md`](docs/spec.md) | O produto: o problema, o escopo e as regras da conversa |
| [`docs/arquitetura.md`](docs/arquitetura.md) | Como um turno funciona: gates, Jev 1, 2 e 3, estados, ações e entrada em fases |
| [`docs/arquitetura/agente-inadimplente-jev.excalidraw`](docs/arquitetura/agente-inadimplente-jev.excalidraw) | O desenho da arquitetura (abra em [excalidraw.com](https://excalidraw.com)) |
| ⭐ [`docs/ideia-webchat.md`](docs/ideia-webchat.md) | **Aprovada (D32):** atendimento humano por webchat no lugar de outro WhatsApp |
| ⭐ [`docs/ideia-waha.md`](docs/ideia-waha.md) | **Provável, não implementada (C6):** o humano num número WAHA no Chatwoot, sem link, no lugar do webchat |
| [`docs/decisoes.md`](docs/decisoes.md) | Decisões numeradas (D1, D2, …), com data |
| [`docs/duvidas.md`](docs/duvidas.md) | Perguntas em aberto, separadas por quem responde |
| [`docs/comparacao-llm.md`](docs/comparacao-llm.md) | Por que o redator é o `gpt-6-luna`: as duas rodadas de comparação dos LLMs, com os resultados brutos |
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
uv run agente simular tests/conversas                     # roda as conversas simuladas (Jev falso)
uv run agente simular tests/conversas --jev-real          # o mesmo com o Jev da TypeSafe (AGENTE_JEV_API_KEY); só texto inventado
uv run agente comparar-redatores tests/conversas --modelo gpt-6-luna:0.10:0.50  # compara os LLMs (T6); exige AGENTE_LLM_API_KEY
```

## Pré-sombra: reproduzir conversas reais

```bash
uv run agente reproduzir --carteira pre-sombra/carteira.csv --dias 3 --saida pre-sombra/2026-10-30
```

O comando lê do Chatwoot as conversas da inbox dos últimos dias (`AGENTE_CHATWOOT_URL`, `_CONTA`,
`_INBOX` e `_TOKEN`; o token é de um usuário só da inbox) e as passa, em ordem de tempo, pelo mesmo
caminho do serviço, em modo sombra e com o Jev falso. **Só lê o Chatwoot** (GET): nada é enviado nem
escrito lá, e o áudio não é transcrito. Ele imprime o relatório agregado (contagens, sem texto nem
telefone) e grava na pasta de `--saida`:

- `reproducao.db`: o banco SQLite da reprodução (um banco novo a cada execução; o comando recusa o que já
  existe);
- `folha.md`: a folha de revisão, com até 50 turnos sorteados (`--amostra`), cada um com a mensagem do
  cliente, o que o agente teria feito e a resposta seguinte do atendente, para dar o veredito (`ok`,
  `errado` ou `perigoso`) e uma nota.

Os dois arquivos têm dado de cliente. A folha mascara telefone, CPF e o nome do contato, mas o texto
continua sendo conversa de cliente, e o banco guarda tudo sem máscara. **Mantenha-os na pasta
`pre-sombra/`, que fica fora do git (o repositório já foi público, e apagar um arquivo depois não limpa o
histórico), e não os envie a lugar nenhum.** Durante
a reprodução, o log também não leva o stack trace nem a mensagem das exceções, só o tipo do erro.

Sem a lista de inadimplentes da unidade, `uv run agente estimar-carteira --saida pre-sombra/carteira-estimada.csv`
(com `--da-pasta` para JSONs já baixados, ou `--dias N` para ler o Chatwoot) estima a carteira das próprias
conversas: a mensagem de cobrança da empresa cita o débito ou o valor do Pix, e o valor dividido pela
mensalidade da política dá as mensalidades em aberto. O CSV sai no formato do importador e o terminal só
mostra contagens. É uma estimativa, não vem do sistema do CDT: use o `--carteira-estimada` no
`reproduzir` para a folha levar o aviso. O CSV tem telefone e nome de cliente e fica em `pre-sombra/`.

## Deploy

O serviço exige o Chatwoot real (T3) e a chave do Luna: sem eles não sobe. Só conversa quem está na
`liberacao` do `politica.toml` (o piloto, com os telefones da lista, D61); quem está fora é ignorado,
sem Luna e sem mensagem, e a lista vazia (o padrão) não conversa com ninguém.

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

## LLM e transcrição do áudio (T6, T11)

O redator é o `gpt-6-luna` da OpenAI (D42) e o áudio do cliente é transcrito pelo
`gpt-4o-mini-transcribe` (D43). Sem as chaves, o serviço roda com as falsas: o redator falso e o áudio
sem leitura (cai no pedido de texto). Para ligar:

- `AGENTE_LLM_API_KEY` liga o redator. `AGENTE_LLM_URL`, `AGENTE_LLM_MODELO` e `AGENTE_LLM_CORPO_EXTRA`
  já vêm com os padrões do Luna. O Luna recusa a temperatura sem o `reasoning_effort: "none"`, que o
  corpo extra padrão manda;
- `AGENTE_JEV_API_KEY` liga o Jev da TypeSafe (T10); `AGENTE_JEV_URL` e `AGENTE_JEV_MODELO` (padrão
  `jev-1.14.0`) trocam o endereço e o modelo. Com a chave, o serviço e o `agente reproduzir` só rodam com
  `AGENTE_JEV_DADOS_REAIS_LIBERADOS=true` (padrão `false`), que espera a TypeSafe confirmar ZDR e No
  Training por escrito (D12); o `agente simular --jev-real` não precisa dela, pois usa texto inventado;
- `AGENTE_TRANSCRICAO_URL` e `AGENTE_TRANSCRICAO_API_KEY` ligam a transcrição, e
  `AGENTE_TRANSCRICAO_MODELO` troca o modelo. Com só uma das duas, o serviço não sobe e diz qual falta;
- os limites do áudio e os textos do pedido de texto estão no `politica.toml`.

O passo a passo está em [`docs/integracoes.md`](docs/integracoes.md) (T6 e T11).

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
