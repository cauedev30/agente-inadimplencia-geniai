# O agente no dev2 (modo teste)

Três contêineres no projeto `agente-inadimplencia`: o `agente` (o serviço, com a migração no
início), o `postgres` (16, sem porta para fora) e o `tunel` (a Cloudflare, que dá ao agente uma URL
pública para o webhook do Chatwoot).

## Antes de subir

- Docker com o plugin `docker compose` no servidor.
- O servidor é **compartilhado** (7,6 GB). Os limites do compose somam 1152 MB (agente 512, Postgres
  512, túnel 128): não suba mais que isso, nem aumente os limites sem avisar quem divide a máquina.
- As credenciais já rotacionadas: o token do Agent Bot, a chave do Luna e a SSH.
- A decisão da retenção de dados da OpenAI (T6). Sem ela, o serviço só sobe com
  `AGENTE_LLM_DADOS_REAIS_LIBERADOS=true`, e esse valor só vale depois dela.

## Onde fica cada arquivo no servidor

```
/opt/agente-inadimplencia/
  agente.env              segredos do serviço (fora do git; chmod 600), a partir de deploy/dev2/agente.env.exemplo
  politica-teste.toml     a política do teste com os telefones do piloto (fora do git), a partir de deploy/dev2/politica-teste.exemplo.toml
  app/                    o código (o conteúdo do `git archive`); daqui sai o compose e o cardapio-teste.toml
    deploy/dev2/.env      POSTGRES_USER, POSTGRES_PASSWORD e POSTGRES_DB (fora do git)
```

O `.env` do compose tem só estas três variáveis, e o usuário, a senha e o banco têm de ser os mesmos
da `AGENTE_DATABASE_URL` do `agente.env` (host `postgres`, banco do `POSTGRES_DB`):

```
POSTGRES_USER=agente
POSTGRES_PASSWORD=<uma senha longa>
POSTGRES_DB=agente
```

O Postgres também cria o banco `agente_teste` (para os testes da tarefa 2.2) quando o volume nasce.

## Subir

Dentro de `/opt/agente-inadimplencia/app`:

```
docker compose -f deploy/dev2/compose.yaml up -d --build
docker compose -f deploy/dev2/compose.yaml ps
```

Se o `agente` não ficar de pé, o log diz o motivo (variável faltando, política inválida, trava da
OpenAI). Para o webhook, leia a URL do túnel (abaixo) e cadastre-a no Chatwoot como
`https://<url-do-tunel>/webhooks/chatwoot/<AGENTE_WEBHOOK_SEGREDO>`.

## Ler a URL do túnel

```
docker compose -f deploy/dev2/compose.yaml logs tunel | grep trycloudflare
```

O túnel rápido **muda de URL toda vez que o contêiner `tunel` recomeça**; depois de reiniciá-lo,
leia a URL de novo e atualize o webhook no Chatwoot.

## Atualizar o código

Na máquina de desenvolvimento, de dentro de `repo/`: `git archive --format=tar.gz -o agente.tar.gz HEAD`.
Copie o arquivo para o servidor, extraia sobre `/opt/agente-inadimplencia/app/` (o `deploy/dev2/.env`
não está no arquivo, então fica) e rode de novo o `up -d --build`. O banco e o volume continuam.

## Ver o log

```
docker compose -f deploy/dev2/compose.yaml logs -f agente
docker compose -f deploy/dev2/compose.yaml logs --tail 200 postgres
```

O `/saude` do serviço (`curl http://127.0.0.1:8000/saude`, de dentro do servidor) dá 503 se o laço
dos turnos parou há mais de 60 segundos.

## Testes do Postgres

Os testes de `tests/postgres/` (o `alembic upgrade head` num banco vazio e os comandos do modo teste
`/caso`, `/paguei`, `/paguei n` e `/nota`, mais a corrida do 1º contato) só rodam com a variável
`AGENTE_TESTE_POSTGRES_URL`; sem ela, são pulados. Eles usam o banco `agente_teste`, que o Postgres do
compose cria quando o volume nasce (`initdb/01-banco-de-teste.sql`), e **apagam o schema `public` dele
a cada teste**: nunca aponte a variável para o banco `agente`. (Um volume criado antes do
`initdb` não tem o `agente_teste`; crie com
`docker compose -f deploy/dev2/compose.yaml exec postgres psql -U agente -c "CREATE DATABASE agente_teste"`.)

O compose não publica a porta do Postgres, e a imagem do agente não tem o `pytest` (é `--no-dev`).
Por isso o teste roda num contêiner descartável na rede do compose, com o código montado, que instala as
dependências de dev e acessa o banco pelo nome `postgres` (**a confirmar no dev2**: nunca rodou lá).
De dentro de `/opt/agente-inadimplencia/app`, com o usuário e a senha do `deploy/dev2/.env` (a senha
sem `%`, que o Alembic lê como interpolação):

```
docker run --rm --network agente-inadimplencia_default -v "$PWD":/app -w /app   -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e PYTHONDONTWRITEBYTECODE=1   -e AGENTE_TESTE_POSTGRES_URL='postgresql+psycopg://agente:<senha>@postgres:5432/agente_teste'   python:3.12-slim sh -c "pip install -q uv && uv sync --frozen && uv run pytest -p no:cacheprovider tests/postgres"
```

A rede se chama `<projeto>_default`; se o nome não bater, `docker network ls` mostra o certo. Os 8
testes passando (nenhum pulado) é o "pronto" da tarefa 2.2.

## Parar e apagar

```
docker compose -f deploy/dev2/compose.yaml stop                  # para e mantém o banco
docker compose -f deploy/dev2/compose.yaml down                  # remove os contêineres, mantém o volume
docker compose -f deploy/dev2/compose.yaml down --volumes        # remove também o banco (apaga as conversas)
```

Se o `.env` do compose não estiver ao lado dele, o `compose` reclama das variáveis do Postgres: é
de propósito, para ninguém subir um banco sem senha.
