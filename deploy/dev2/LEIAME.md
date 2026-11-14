# O agente no dev2 (modo teste)

Três contêineres no projeto `agente-inadimplencia`: o `agente` (o serviço, com a migração no
início), o `postgres` (16, sem porta para fora) e o `tunel` (a Cloudflare, que dá ao agente uma URL
pública para o webhook do Chatwoot).

## Antes de subir

- Docker com o plugin `docker compose` no servidor.
- O servidor é **compartilhado** (7,6 GB). Os limites do compose somam 1152 MB (agente 512, Postgres
  512, túnel 128): não suba mais que isso, nem aumente os limites sem avisar quem divide a máquina.
- A chave do Luna no `agente.env`. A rotação da SSH, do token do Agent Bot e da OpenAI antiga ficou para
  depois (decisão do Cauê, 2026-11-11); quando rotacionar a OpenAI, troque a chave aqui também.
- `AGENTE_LLM_DADOS_REAIS_LIBERADOS=true` só no teste (3 testadores, casos fictícios, sem a chave do CTN).
  A conferência da retenção da OpenAI (T6) é exigência **antes do piloto real**.

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

## Apontar o Chatwoot para o túnel (`configurar_chatwoot.py`)

Em vez de editar o webhook à mão, rode na máquina de desenvolvimento, de dentro de `repo/`:

```
python -m uv run python ferramentas/teste/configurar_chatwoot.py            # só mostra (dry-run)
python -m uv run python ferramentas/teste/configurar_chatwoot.py --aplicar  # escreve
```

O script lê do `dev2`, por SSH, a URL atual do túnel e o `AGENTE_WEBHOOK_SEGREDO`; do Chatwoot lê o que
já está cadastrado; os tokens vêm de `secrets/chatwoot-teste.local.json`. Com `--aplicar` ele troca a URL
do **webhook 14** e o `outgoing_url` do **bot 69** da conta 91. Não mexe no bot 67 (do Tiago) nem no
`webhook_url` da inbox 280 (do gateway da GeniAI). Nada secreto é impresso. **Rode de novo toda vez que o
contêiner `tunel` recomeçar.**

Dois fatos da conta 91: ela **não tem time**, então `AGENTE_CHATWOOT_TIME_HUMANO` fica vazio (o handoff
abre a conversa, etiqueta e põe a nota, sem atribuir); e o webhook 14 é da **conta toda**: o
agente ignora o que chega de outra inbox (`AGENTE_CHATWOOT_INBOX=280`) e, no modo teste, sem carteira, não grava a
mensagem de quem está fora do piloto (`sem_cobranca`) nem responde.

## Particularidades do teste no dev2

- **Janela aberta 24 h, sem feriado.** A `politica-teste.toml` (e o `politica-teste.exemplo.toml`, de onde
  ela sai) tem a janela `[janela.dias]` de segunda a domingo, `00:00` a `23:59`, e `feriados = []`; o
  `repo/politica.toml` real não mudou (seg-sex 8h-18h, sáb 8h-12h, D11/D16). Sem isso o agente não responde depois das 18h,
  de propósito. Antes do piloto real, volte a janela ao padrão (a cópia `politica-teste.toml.bak-janela`
  guarda a versão anterior). Um "Oi" que chegou fora da janela fica pendente e **não volta sozinho**:
  mande `/reset` e depois "Oi" de novo.
- **A política é `chmod 644`**, porque o contêiner roda como uid 1000 e precisa lê-la. O arquivo só tem
  telefones do piloto; o `agente.env` segue `chmod 600`.
- **Foto e figurinha chegam do gateway como texto `[Image]`/`[Sticker]` mais o anexo.** A foto vai ao
  atendente (`CONCILIACAO`, D63); só a figurinha fica sem resposta. A leitura da imagem está em
  `docs/depois.md`. Avise os testadores.
- **O token do Agent Bot dá 401 em `GET /messages`.** O agente usa essa leitura para conferir se a
  mensagem já saiu; com o 401, a conferência vira "não sei" e a confirmação do pagamento sai mesmo assim
  (`6379d91`; o risco, uma confirmação repetida, está em `docs/depois.md`). Se a confirmação voltar a
  travar, procure `401` no log.
- **Rede com firewall (Fortinet).** Numa rede assim o TLS é interceptado e a porta 22 fica fechada: o
  `ssh dev2` e o Chatwoot não respondem, e o `configurar_chatwoot.py` falha. Troque de rede; não tente
  contornar o firewall.
- **Imagem do túnel:** `cloudflared:latest`. Se o `up` puxar uma versão nova, o formato do log da URL pode
  mudar (o script procura `https://*.trycloudflare.com`).

## Atualizar o código

Na máquina de desenvolvimento, de dentro de `repo/`: `git archive --format=tar.gz -o agente.tar.gz HEAD`.
Copie o arquivo para o servidor, extraia sobre `/opt/agente-inadimplencia/app/` (o `deploy/dev2/.env`
não está no arquivo, então fica) e rode de novo o `up -d --build`. O banco e o volume continuam. **Não suba no meio de um caso:** o estado
dos casos (os meses pagos com `/paguei`) vive na memória, e o reinício devolve ao testador o mês que ele pagou.

Antes do `up`, compare a `politica-teste.toml` do servidor com o `deploy/dev2/politica-teste.exemplo.toml` do código
novo: tire a chave que saiu e ponha a que entrou. A política recusa chave desconhecida e exige as obrigatórias, então
o serviço não sobe com a cópia velha. O processo que está rodando leu a política na subida, e editar antes do `up` é
seguro.

## Ver o log

```
docker compose -f deploy/dev2/compose.yaml logs -f agente
docker compose -f deploy/dev2/compose.yaml logs --tail 200 postgres
```

O `/saude` do serviço dá 503 se o laço dos turnos parou há mais de 60 segundos. Como o compose não
publica porta, consulte de dentro do contêiner:
`docker compose -f deploy/dev2/compose.yaml exec agente python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/saude').read())"`.

## Portas

**Este projeto não usa nenhuma porta do servidor.** O `agente` só `expose` a 8000 dentro da rede do
compose (o túnel da Cloudflare chega por ali), e o Postgres também não publica porta. Assim não
disputamos porta com ninguém. A 8000 do host é do Caddy do gateway (outro projeto): não publique nada
nela. Se um dia o agente precisar de uma porta no host, escolha uma livre (`ss -ltn`) e rotule aqui e
em `labels:` do compose com o nome do projeto e o motivo.

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
dependências de dev e acessa o banco pelo nome `postgres` (rodou no dev2 em 2026-11-11: 8 passaram).
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
