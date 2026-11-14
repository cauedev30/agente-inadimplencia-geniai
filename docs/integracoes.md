# Guia de integração: ligar os sistemas reais

O agente está pronto em código, no corte de 2026-11-11 (D59, D60, D61), mas roda com **implementações falsas** dos
sistemas de fora, salvo a leitura do CTN. Este guia diz, para cada um, **o que precisamos receber**, **onde entra no código** e **como saber que
ficou pronto**. As perguntas originais estão em [`duvidas.md`](duvidas.md) (T1 a T11).

## Como cada integração funciona

Todo sistema de fora entra por uma **interface** em `src/agente/integracoes/` (um `Protocol` do Python).
Cada interface tem uma implementação falsa, usada nos testes e no desenvolvimento, e um **teste de
contrato** em `tests/integracoes/`. A implementação real:

1. implementa os mesmos métodos da interface;
2. herda a classe de contrato (`ContratoPix` ou `ContratoCTN`) num arquivo de teste e
   define as fixtures que o contrato pede, contra o sandbox ou o ambiente de teste;
3. levanta a exceção de "fora do ar" da interface quando o sistema não responde (rede caída, prazo
   estourado ou 5xx). O turno trata essa exceção: tenta de novo e, depois de `integracao_tentativas`
   (no `politica.toml`), passa a conversa para o humano;
4. é ligada em `montar_dependencias` (`src/agente/api/principal.py`), lida das variáveis de ambiente.
   Sem as variáveis, o serviço continua com a falsa.

Credenciais só por variável de ambiente (`.env`, fora do git). O [`.env.example`](../.env.example)
traz os nomes, sem os valores. O repositório é privado, mas nenhum dado real de cliente entra em teste ou log.

## Resumo

| # | Sistema | Precisamos de | Arquivo novo | Bloqueia |
|---|---|---|---|---|
| T2 | Servidor | o `dev2`, o Postgres e um túnel (o domínio fica para o piloto real) | — (deploy pronto) | subir o teste |
| T3 | Chatwoot do JP Sul | conta, inbox, Agent Bot, token | — (cliente pronto) | subir o teste |
| T1 | Pix do banco do CDT | API, sandbox, webhook | `integracoes/pix_<banco>.py` | tarefa 6.4 |
| T9 | CTN | API, ambiente de teste, webhook | `integracoes/ctn_http.py` | tarefa 6.5 |
| T4 | Disparador | API, templates aprovados | `integracoes/disparador_http.py` | abertura real |
| T6 | LLM (`gpt-6-luna`, D42) | chave, retenção de dados | — (`LLMTurnoOpenAI` pronto) | o Luna real |
| T11 | Transcrição de áudio | chave da OpenAI (D43) | — (`TranscritorOpenAI` pronto) | áudio do cliente |

A ordem que destrava mais: **T2, T3 e T6** (o agente sobe no `dev2` e o testador conversa com o Luna real), depois
**T1, T9 e T4** (pagamento e abertura reais, que abrem o piloto real).

---

## T2 — Servidor

**Precisamos de:** o servidor (D1), um Postgres e o endereço público do serviço, que o Chatwoot e os webhooks vão
chamar. Para o teste é o `dev2`, com o serviço, o Postgres e um túnel; o domínio fica para o piloto real.

**Já pronto:** o `Dockerfile` roda `alembic upgrade head` e sobe o uvicorn. Ver a seção "Deploy" do
[`README`](../README.md).

- `AGENTE_DATABASE_URL=postgresql+psycopg://...`
- a `liberacao` do `politica.toml` (o piloto): lista vazia, ninguém conversa
- o health check consulta `GET /saude`, que não toca no banco e dá 503 se o laço dos turnos não fecha um ciclo há mais de 60 s. `GET /saude/pagamentos` conta o que ficou pendente.

**Pronto quando:** o serviço sobe, as migrações rodam até a última (`alembic upgrade head`) e o `/saude` responde.

**Testar no Postgres** (a suíte roda em SQLite): as travas da confirmação de pagamento e do turno
(`FOR NO KEY UPDATE`, na ordem conversa → cobrança → pagamento) só foram conferidas pelo SQL emitido.
Falta um teste com duas conexões ao mesmo tempo. A importação da carteira e o disparador seguem a mesma
ordem sem travar a conversa (ver o docstring de cada um); a expiração dos Pix trava os pagamentos em
ordem de id.

## T3 — Chatwoot do João Pessoa Sul

**Precisamos de:**
- a URL do Chatwoot, o número da conta e a inbox do WhatsApp que o agente atende;
- o Agent Bot criado nessa inbox, com o token;
- o time dos atendentes humanos (o handoff atribui a ele); o atendente responde na mesma conversa do WhatsApp (D60).

**Variáveis:** `AGENTE_CHATWOOT_URL`, `AGENTE_CHATWOOT_CONTA`, `AGENTE_CHATWOOT_INBOX`,
`AGENTE_CHATWOOT_TOKEN`, `AGENTE_CHATWOOT_TIME_HUMANO` e `AGENTE_WEBHOOK_SEGREDO`.

**Configurar no Chatwoot:**
- o webhook do Agent Bot em `POST /webhooks/chatwoot/{AGENTE_WEBHOOK_SEGREDO}`;
- o rodízio dos atendentes: a atribuição automática do Chatwoot (*a confirmar* na conta do JP Sul, D60). Detalhes
  na seção "Configuração do Chatwoot" do [`README`](../README.md).

**Já pronto:** o `ChatwootHttp` (`integracoes/chatwoot.py`) e o webhook.

**A confirmar com o Chatwoot real:**
- o formato do toque no botão "Pagar com Pix": hoje o gate lê `content_attributes` com o payload
  `PAGAR_PIX` e também o texto do botão;
- o `data_url` do áudio tem a mesma origem (esquema, host e porta) do `AGENTE_CHATWOOT_URL`: o
  `FRONTEND_URL` do Chatwoot manda o endereço público, e com o `AGENTE_CHATWOOT_URL` interno todo áudio
  vira pedido de texto (o log avisa, sem o link).

**Pronto quando:** uma mensagem do cliente no WhatsApp chega ao webhook e o turno responde (ou passa ao atendente).

## T1 — Pix do banco do Cartão de Todos (tarefa 6.4)

**Precisamos de:**
- qual banco, e a confirmação de que ele tem API Pix com cobrança por txid e webhook de Pix recebido
  (D22);
- o sandbox e as credenciais (certificado ou OAuth, conforme o banco);
- se o dinheiro cai na conta da franquia JP Sul ou na do Cartão de Todos;
- o formato e a autenticação do webhook de Pix recebido.

**Implementar** `PixBanco` (`integracoes/pix.py`):

| Método | O que faz |
|---|---|
| `criar_cobranca(txid, valor_centavos, validade, devedor_nome) -> CobrancaPix` | cria a cobrança com o txid nosso; devolve o `br_code` (copia e cola) e o `expira_em` com fuso |
| `consultar(txid) -> StatusPix` | PENDENTE, PAGO ou EXPIRADO |

- O txid vem do `gerar_txid`, com 33 caracteres alfanuméricos.
- O txid repetido levanta `TxidRepetido`; o valor zero ou negativo, `ValueError`.
- O banco fora do ar levanta `PixIndisponivel`.

**Teste:** herdar `ContratoPix` (`tests/integracoes/test_contrato_pix.py`) e definir as fixtures `pix`
e `pix_fora_do_ar`.

**Webhook:** hoje `POST /webhooks/pix` espera um payload provisório, `{referencia, valor_centavos,
pago_em}`. A assinatura é o HMAC-SHA256 do corpo no cabeçalho `X-Assinatura`, com o segredo
`AGENTE_PIX_WEBHOOK_SEGREDO`. Quando o formato real chegar, ajusta-se a leitura em
`api/webhook_pagamento.py`. Atenção:
- banco costuma avisar **todo** Pix recebido na chave. Hoje a referência desconhecida responde 404,
  e provavelmente terá de virar 200 com log, para o banco não reenviar sem parar;
- o `handoff_expira_pix = true` só expira no nosso banco. Para cancelar no banco do CDT, falta um
  método de cancelamento na interface.

**Pronto quando:** um Pix de R$ 0,01 é gerado e pago no sandbox, e o webhook confirma.

## T9 — CTN (tarefa 6.5)

**Atualização de 2026-11-09:** a **leitura** já é real, pela API EFICAZ. Ver a seção
[CTN real (EFICAZ, só leitura)](#ctn-real-eficaz-só-leitura), logo abaixo. O que resta desta T9 é a
**escrita** (link de pagamento, link de cadastro do cartão, baixa e webhook), que a API não tem.

**Como vai ser (2026-10-26):** não teremos acesso ao CTN. O desenvolvedor do Cartão de Todos está
fazendo **uma API só para o agente**, e todos os dados reais passam por ela. Ela vai além da consulta. A
lista abaixo é o que ela precisa oferecer; falta conferir item a item com o dev.

**Precisamos de:**
- confirmar que "CTM" e "CTN" são o mesmo sistema;
- a API: consulta do cliente pela matrícula, **histórico de pagamentos com a forma e a data**, **link
  de pagamento com um valor definido pelo agente** (com desconto, D34 e D35), link só para cadastrar o
  cartão e **registro da baixa**;
- se quem paga pelo link já sai com a baixa no próprio CTN (T9.6);
- o webhook de pagamento pelo link, com o formato e a autenticação;
- o ambiente de teste e uma matrícula de teste.

**Implementar** `CTN` (`integracoes/ctn.py`):

| Método | O que faz |
|---|---|
| `consultar_cliente(matricula) -> ClienteCTN` | nome, telefone e CPF; `ClienteNaoEncontrado` se não existe |
| `historico_pagamentos(matricula, meses) -> list[PagamentoHistorico]` | do mais antigo ao mais novo, com a forma e a data (a propensão, D36) |
| `gerar_link_pagamento(matricula, valor_centavos, cobranca_id) -> LinkPagamento` | o link em que o cliente paga e cadastra o cartão (D30); o `id_externo` é a referência do pagamento |
| `gerar_link_cadastro_cartao(matricula) -> LinkPagamento` | o link só para cadastrar o cartão (G9.3); **sem efeito colateral e pedido quantas vezes for preciso**: a confirmação o pede antes das travas, e o link some se o webhook for repetido ou a janela de 24h fechar na espera |
| `registrar_baixa(matricula, valor_centavos, pago_em, referencia) -> bool` | a baixa na aba de mensalidades; **idempotente pela referência**: devolve False se já tinha baixa |

O CTN fora do ar levanta `CTNIndisponivel`.

**A cuidar:** a confirmação do pagamento (`pagamento/confirmar.py`) pede o link do convite ao CTN antes
das travas, dentro da transação do webhook, que não segura trava de linha mas segura a conexão do banco
enquanto o CTN responde. Dois webhooks iguais em paralelo pedem o link os dois, e um deles o descarta.
Se o CTN real for lento ou o link tiver efeito, o certo é gerar o convite na 2ª fase
(`pagamento/concluir.py`), na hora de enviar a confirmação, e não na 1ª.

**Teste:** herdar `ContratoCTN` (`tests/integracoes/test_contrato_ctn.py`) e definir as fixtures
`ctn_e_matricula` e `ctn_fora_do_ar_e_matricula`.

**Parâmetro a ajustar:** `agente_da_baixa_no_link` no `politica.toml`. `true` quer dizer que o agente
registra a baixa do pagamento pelo link; `false` quer dizer que o próprio CTN dá a baixa. Hoje está
`false`.

**Webhook:** `POST /webhooks/ctn`, igual ao do Pix, com o segredo `AGENTE_CTN_WEBHOOK_SEGREDO`.

**Pronto quando:** o histórico, o link com desconto e a baixa funcionam numa matrícula de teste.

## CTN real (EFICAZ, só leitura)

**Desde 2026-11-09 a leitura do CTN é real.** O dev da GeniAI liberou a API EFICAZ, que só tem `GET`.
Com a chave, o agente lê a carteira, a dívida, o histórico de pagamento e as baixas. O link do cartão, o
link de cadastro e o `registrar_baixa` **seguem falsos**: a API não tem escrita (T9.15 em
[`duvidas.md`](duvidas.md)). Decisões: D48 a D53 em [`decisoes.md`](decisoes.md). Spec do workspace:
`specs/2026-11-06-integracao-ctn.md`. Documentação da API e sondagens: `fontes/2026-11-04-api-eficaz-ctn.md`.

### As rotas lidas

Todas são `GET` em `https://api.eficaz-group.com` (`AGENTE_CTN_URL`), com `unidade` e `ordem` em toda
chamada.

| Rota | O que o agente tira dela |
|---|---|
| `/v1/ctn/inadimplencia` | o retrato diário da carteira: matrícula, nome, telefone, `meses_atraso`, `meses_ate_corte`, `ult_mesref`, `data_ref`. Um retrato por dia, por volta das 07h05 (Brasília). **Não traz valor** |
| `/v1/ctn/lancamentos` | uma linha por mensalidade. As de `status_lancamento = rejeitada` são os meses em aberto, e o `valor` delas é a dívida exata. Não muda no mesmo dia |
| `/v1/ctn/lancamentos_bx` | as baixas (`forma`, `mes_ref`, `valor`, `dt_baixa`, `coletado_em`). Alimenta o histórico de pagamento (D36) e a notícia de que o cliente pagou. O CTN as coleta a cada 10 minutos, com até ~2h45 de atraso |
| `/v1/ctn/filiados_full` | o cadastro do cliente (nome, telefone, CPF), para o `consultar_cliente` |

O `inadimplencia_resumo` (totais por régua) **não é lido pelo agente**. Serve só de conferência manual.
Em 09/11 ele mostrou ~2.000 pessoas de 1 e 2 meses que o `inadimplencia` não traz (T9.7).

O que o agente faz com elas:
- **Régua:** sai do `meses_atraso` (D25), não do campo `regua` da API, que mede outra coisa.
- **Dívida do retrato:** `meses_atraso × mensalidade` (D24, `mensalidade_centavos` no `politica.toml`). É
  estimativa, e nunca se cita ao cliente sem a conferência (abaixo).
- **Dívida conferida:** a soma do `valor` das `rejeitada`, menos as pagas depois da recusa. Cada baixa paga
  um lançamento só: o do mesmo mês, de valor igual ou maior e de data igual ou posterior. A parcial não
  tira o mês.

### Variáveis

| Variável | Para que serve |
|---|---|
| `AGENTE_CTN_API_KEY` | a chave `eg_...`. Presente liga o CTN real; ausente ou vazia, o serviço usa o `CTNFalso`. Nunca vai ao git, ao log nem ao chat |
| `AGENTE_CTN_URL` | a base da API. Padrão `https://api.eficaz-group.com`; vazia volta ao padrão |
| `AGENTE_CTN_UNIDADE` | a unidade do CTN. Padrão `jp`; vazia volta ao padrão. *A confirmar* que `jp` é o JP Sul da D17 (T9.14) |
| `AGENTE_LLM_DADOS_REAIS_LIBERADOS` | a trava do M2 (abaixo). Padrão `false` |

A demonstração não recebe a chave e fica no `CTNFalso`.

### A trava dos dados reais

Com uma **fonte de dado real** (`AGENTE_CTN_API_KEY` ou o Chatwoot real) **e** um **destino externo** do
texto do cliente (`AGENTE_LLM_API_KEY`, o Luna, ou `AGENTE_TRANSCRICAO_*`, a transcrição), o serviço
**recusa subir** enquanto `AGENTE_LLM_DADOS_REAIS_LIBERADOS` não for `true`. A mensagem do erro diz quais
variáveis dispararam, nunca os valores. Só se libera depois de a retenção de dados da OpenAI ser conferida
no painel (T6, D42). Ela fecha o M2: dado real de cliente não vai à
OpenAI sem a conferência da retenção. Como o serviço sempre exige o Chatwoot real e a chave do Luna, na prática ele só
sobe com a liberação em `true`.

### O `CTNComposto`

`CTNComposto(leitura, escrita)` implementa o `Protocol CTN` inteiro, então nenhum chamador muda. As
leituras vão ao `CTNEficaz` (`integracoes/ctn_eficaz.py`); o link de pagamento, o link de cadastro do
cartão e a baixa vão ao `CTNFalso`. O `principal.py` o monta quando há a chave. Os métodos novos de
leitura são `inadimplentes(data_ref)`, `mensalidades_em_aberto(matricula)`, `consultar_mensalidades` (que
também diz quantas rejeitadas leu) e `baixas_desde(coletado_desde)`.

Regras do cliente `CTNEficaz`:
- só leitura, transporte injetável, prazo total de 12 s por consulta (a API corta em 15 s);
- o erro vira `CTNIndisponivel` **sem URL, sem chave e sem corpo** (o corpo traz dado de cliente);
- **qualquer HTTP de erro**, o 400 inclusive, vira `CTNIndisponivel`; só o 404 do cadastro vira
  `ClienteNaoEncontrado`;
- a matrícula que a API não conhece devolve **lista vazia** no histórico e nas mensalidades (a API não
  distingue "sem baixas" de "sem a matrícula"); só o `consultar_cliente` levanta `ClienteNaoEncontrado`;
- a paginação segue até `mais = false` e **falha fechada**: `mais` que não é booleano, página vazia com
  `mais` e mais de 200 páginas numa consulta levantam `CTNIndisponivel`. Nunca devolve um resultado cortado
  que pareça inteiro;
- uma linha malformada do retrato é pulada e contada; a falha geral de formato levanta;
- as baixas de uma pessoa ficam em cache por 60 segundos (a conferência as usa duas vezes).

### O limite de taxa

A chave tem **60 chamadas por minuto, 5.000 chamadas e 20.000 linhas por dia**. A página é de **50 linhas**:
um `limite` maior é ignorado (o teto da chave). Chamada recusada também conta. O cliente:
- segura **55 chamadas por minuto** (sobra folga para o relógio e para a recusa), seguro entre threads;
  quem passa do limite dorme até a mais antiga sair da janela, e a espera tem teto próprio;
- guarda o campo `uso` do corpo e o põe no log em DEBUG;
- um retrato inteiro custa ~58 chamadas e ~2.900 linhas (09/11). Cada conferência custa 2 chamadas (os lançamentos e as baixas da pessoa), mais as
  páginas extras de quem tem mais de 50 linhas.

### A carteira do dia

```
agente importar-ctn [--data AAAA-MM-DD] [--campanha ctn] [--meses-minimos N] [--simular]
```

Lê o retrato do dia (padrão: hoje, no fuso da janela) e grava como o `importar` do CSV: o upsert do
`Cliente` e da cobrança ATIVA, com a régua por `dominio/divida.py`. A propensão ao cartão **não** sai
daqui (custaria 1 chamada por cliente): sai na conferência. A saída traz só contagens.

- **Entra** quem tem `meses_atraso ≥ 1` e telefone. Energisa (`meses_atraso = 0`) fica de fora e é contada
  em `ignoradas Energisa`; sem telefone é contado e não entra.
- **A leitura vem antes da gravação.** O retrato inteiro é lido para a memória; se o CTN cair no meio,
  nada é gravado nem encerrado (saída 1, "rode de novo"). A gravação é em lotes de 50, com commit por lote,
  e é idempotente.
- **Encerra** a cobrança ATIVA **da mesma campanha** que saiu do retrato (`encerrada_motivo =
  saiu_do_retrato`), sem mensagem. Não encerra se o retrato for suspeito (linha de outra `data_ref`, ou
  nenhuma da data pedida), vazio, ou se o encerramento atingir **mais de 25%** das ativas da campanha (só
  vale com ao menos 20 ativas). A cobrança com Pix nosso pendente fica ATIVA até o Pix expirar ou ser pago.
- **Reativa** a cobrança encerrada por `saiu_do_retrato` que volta ao retrato **em até 3 dias** (no fuso
  da janela), em vez de criar outra. A conversa se mantém. Cobrança encerrada por outro motivo e a PAGA
  nunca voltam.
- `--meses-minimos N` só deixa entrar quem tem N meses de atraso ou mais (padrão 1; no `dev2`, 3: R3 a R6).
- `--simular` faz tudo e desfaz: mostra os totais que a execução real teria.

**Códigos de saída:**

| Código | Quando |
|---|---|
| 0 | importou e o encerramento rodou |
| 1 | retrato incompleto (o CTN caiu na leitura): nada foi gravado |
| 2 | sem a `AGENTE_CTN_API_KEY` |
| 3 | importou, mas **não encerrou** por retrato suspeito, vazio ou encerramento acima de 25%: o `motivo de não encerrar` vai na saída e pede revisão |

### A conferência no contato

A carteira do retrato só tem a contagem de meses. Antes do 1º envio **do dia** a um cliente, 2 chamadas
(os lançamentos e as baixas dele) trazem os meses e o valor exatos. A conferência acontece em dois
lugares, **antes das travas de linha**: o disparo (`abertura/disparar.py`) e o 1º turno
(`turno/orquestrador.py`, só com o CTN real e só quando os gates já decidem seguir ou gerar o Pix). Ela
grava `conferida_em`, `meses_em_aberto` (lista `AAAA/MM`, dado para o A20), a dívida, os meses e a
propensão ao cartão (D36). É **uma vez por dia por cobrança** (no fuso da janela).

**O `ult_mesref` só desempata (D55).** A importação guarda na cobrança o `ult_mesref` do retrato (coluna
`cobranca.ult_mesref`, só ela a altera; o CSV deixa nulo). Se as abertas do CTN são críveis, nada é filtrado. Só
quando seriam `dado_suspeito` (mais de 1 mês além do retrato, por exemplo) e há `ult_mesref`, a conferência olha as
abertas de mês estritamente posterior a ele (o mês dele fica fora) e as aceita **só se forem exatamente os meses do
retrato** e críveis; senão segue `dado_suspeito`. Sem `ult_mesref`, segue `dado_suspeito`. A quitação (D54) e o
`sem_lancamentos` não usam o `ult_mesref`. O filtro nunca vale sozinho porque em 2 de 35 cobranças reais o
`ult_mesref` contradizia os lançamentos.

**Na mesma rodada a reconferência só encolhe a dívida (D55).** Quando o cliente paga os meses recentes, os
antigos (anteriores ao `ult_mesref`) seguem abertos no CTN, e a reconferência da rotina das baixas (a cada 10
minutos) veria outro quadro: com o retrato de 3 meses e 3 antigos abertos, pagar 1 recente deixaria 5 abertas
(suspeitas, e o filtrado daria 2, não 3); pagar 2 deixaria 4 (críveis, cobrando 3 antigos); pagar os 3 deixaria os
3 antigos abertos e a cobrança nunca quitaria. Por isso, a cobrança **já conferida hoje** (`conferida_em` de hoje,
no fuso da janela) **com `meses_em_aberto` gravado** considera só os meses abertos que **já estavam nessa lista**: o
mês pago sai, pagos todos a lista fica vazia e a rotina quita (as rejeitadas lidas ainda precisam cobrir o retrato,
D54), e os antigos nunca entram. Um mês que apareça em aberto no meio do dia só entra na importação seguinte (lado
seguro: cobra menos, nunca mais). A regra não depende do `ult_mesref`. Fora dessa condição (nunca conferida, ou
conferida em outro dia) vale a regra anterior, com o desempate do `ult_mesref`. O teste de dado suspeito usa como
patamar os meses do retrato (`mensalidades_retrato`), e não `mensalidades_abertas`, que a própria conferência
sobrescreve (o patamar subiria 1 a cada conferência). **Reimportar por CSV zera o `ult_mesref`** de uma carteira que
veio do CTN (o CSV não o traz): sem ele não há desempate, e abertas suspeitas seguem `dado_suspeito` (lado seguro:
não cobra).

| Estado | Quando | O disparo | O turno ativo |
|---|---|---|---|
| `CONFERIDA` | o CTN respondeu e o dado é crível | envia, com o valor conferido | cobra com o valor conferido |
| `QUITADA` | havia rejeitadas, todas têm baixa e elas cobrem os meses do retrato (D54) | não envia; a cobrança fica ATIVA | não cobra e leva ao humano (handoff `CONCILIACAO`, a mensagem só na 1ª vez) |
| `SEM_LANCAMENTOS` | nenhuma rejeitada, mas o retrato diz que há atraso | não envia | espera como a integração fora do ar; esgotadas as tentativas, vai ao humano (`INTEGRACAO_INDISPONIVEL`) |
| `DADO_SUSPEITO` | mês repetido, valor zero, negativo ou acima de R$ 1.000, mais de 1 mês aberto além do retrato (salvo o desempate do `ult_mesref`, D55), ou rejeitadas todas pagas mas em menor número que os meses do retrato (D54) | não envia | idem |
| `NAO_CONFERIDA` | o CTN caiu | não envia | idem |

Regra de ouro: **só `CONFERIDA` deixa citar valor.** O valor do retrato é estimativa e nunca vai ao
cliente.

**Alarme do disparo:** se, numa rodada, mais de **30%** das conferências feitas (com o mínimo de **20**)
forem `QUITADA` ou `SEM_LANCAMENTOS`, a carteira ou o CTN está errado. A rodada **para na hora**, marca
`alarme_conferencia` e o `agente disparar` sai com o código **3**. Confira o CTN e a carteira antes de
rodar de novo.

### As baixas externas

Quem paga por outro caminho (boleto, cartão, balcão) aparece no `lancamentos_bx`. Uma rotina do serviço,
**a cada 10 minutos** e só com o CTN real e as dependências de pagamento, lê as baixas novas e aplica:

- **Leitura:** `baixas_desde(cursor)`, com o cursor `baixas` na tabela `cursor_ctn` (a `coletado_em` da
  última baixa lida; inclusivo, por isso idempotente). Sem cursor, a 1ª leitura recua 3 horas. A rotina não
  levanta se o CTN ou a gravação falharem: o cursor não anda. Baixa com campo acima do tamanho da coluna é
  pulada, mas deixa o rastro `malformada`.
- **Registro:** cada baixa vai para a tabela `baixa_ctn`, única pelo `id` da API. O campo `efeito` diz o que
  a rotina fez (`quitou`, `abateu`, `ignorada`, `ja_paga`, `sem_cobranca`, ...). A baixa que não pôde ser
  tratada porque o CTN estava fora fica sem `aplicada_em` e é refeita no ciclo seguinte, por até 24 horas;
  sem lançamentos, com dado suspeito ou com a matrícula que o CTN não conhece, a resposta não muda, e a baixa
  encerra na hora (`nao_conferida`) sem parar as outras. Uma matrícula que levanta erro depois de chamar o CTN
  gasta o teto e desiste em 24 horas. Por
  passada a rotina confere no máximo 10 matrículas (as demais ficam para o ciclo seguinte) e para no 1º CTN
  fora, para não estourar a cota (60 chamadas por minuto).
- **Quitou** (a conferência achou rejeitadas e nenhuma em aberto): a cobrança vira **PAGA** com motivo
  `baixa_externa`, a conversa encerra e sai **1 aviso**, o `texto_baixa_externa` do `politica.toml`, texto
  fixo, **só dentro da janela de 24h** do WhatsApp e fora do teto de mensagens. Fora da janela ou com humano na
  conversa, não sai aviso (com humano, o atendente recebe uma nota privada, refeita nos ciclos
  seguintes por até 24 horas se o Chatwoot falhar). O reenvio de um aviso que ficou pendente repete esses
  testes: se o telefone saiu do piloto ou um atendente assumiu, o aviso é descartado.
- **Pagou parte:** os meses, a dívida e a régua saem da conferência e o agente segue, sem mensagem própria.
  O Pix e o link pendentes de valor diferente da dívida nova expiram, para o mês pago por fora não entrar
  duas vezes.
- **Ignoradas:** a baixa do mês corrente (ou futuro) que **não** está entre os meses em aberto da cobrança
  (ainda não venceu); se o mês corrente está em atraso, ou a cobrança ainda não tem a lista de meses, a baixa é
  conferida. A cobrança já PAGA (D39) e o cliente sem cobrança ATIVA são só registrados.
- Na quitação, um Pix nosso pendente não é tocado. Se for pago depois, **e** a baixa que quitou for a dele (nenhum
  outro pagamento nosso pago, e alguma baixa da cobrança com `pago_em` a menos de 1 dia do dele), ele só fica pago,
  sem aviso de dobro; se não, cai no fluxo do pagamento em dobro (D39). Padrão assumido: D54.
- **Baixa malformada** (campo acima da coluna): não é tratada, mas deixa uma linha com `efeito = malformada` e
  entra em `puladas` do `ResumoBaixas`; o cursor passa por ela.
- A chamada ao Chatwoot (aviso e nota) vem sempre depois do commit; as travas de linha seguem a ordem do
  webhook do Pix (conversas, cobrança, pagamento; `db/travas.py`).
- Nenhum log leva matrícula, nome nem telefone, nem o texto do erro do SQL (traz os parâmetros): só o tipo
  do erro e ids do banco.

### O telefone com e sem o 9 (A1)

O CTN dá o celular com o 9, e o WhatsApp e o Chatwoot às vezes mandam sem. O agente guarda o telefone como
o CTN dá e casa nos dois formatos: `variantes_telefone(e164) -> frozenset[str]` (`dominio/telefone.py`)
devolve as duas escritas do celular. O fixo volta só ele mesmo, e o que não é brasileiro não tem variante,
então nada casa com ele. É usada no webhook do Chatwoot (`chatwoot_webhook.py`), na liberação do piloto
(`turno/liberacao.py`), nos gates (`turno/gates.py`) e na checagem de telefone compartilhado do disparo.

### A forma de pagamento

O histórico de pagamento usa a `forma` da baixa:

| `forma` no CTN | `FormaHistorico` |
|---|---|
| `MASTERCARD`, `VISA`, `ELO`, `HIPERCARD` (e outras bandeiras) | `CARTAO` |
| `PIX` | `PIX` |
| começa com `BOLETO` | `BOLETO` |
| `ENERGISA` | `ENERGIA` |
| `DIRETO NO CARTÃO - DINHEIRO` | `OUTRO` (decisão do Cauê) |
| `CAIXA ECONOMICA FEDERAL` | `OUTRO` (padrão assumido; é boleto? pergunta ao dev, T9.11) |
| qualquer outra | `OUTRO` |

O `DIRETO NO CARTAO - CORPORATIVO` cai em `CARTAO` (o nome tem "CARTAO").

### O ensaio com a API real (2026-11-10, parcial)

Feito com um script que já saiu do repositório (`ferramentas/ctn/ensaio_sombra.py`, na tag
`antes-do-enxugamento`): banco descartável, Chatwoot falso (nada sai), só contagens. Feito: importar, baixas (1
passada) e conferir 35 cobranças. **Falta** medir o atraso das baixas em horário comercial, que decide a
`MARGEM_DE_RELEITURA` (hoje zero, em `pagamento/baixas_ctn.py`).

- **Cota:** a da chave volta a zero na virada do dia (o campo `uso` mostrou `linhas_hoje=50` às 00:21 de 10/11).
  Importar o retrato custou 58 chamadas e 2.898 linhas; cada conferência, cerca de 3 chamadas e 80 linhas.
- **Retrato do dia:** às 00:21 ainda não existe (sai por volta das 07h05): a importação devolveu `retrato_vazio` e
  não encerrou ninguém. O `importar-ctn` agendado tem de rodar depois desse horário.
- **Importação:** o retrato de 09/11 deu 2.898 linhas, 2.765 distintos com 1 mês ou mais e 133 da Energisa, como
  na PARADA 2.
- **Conferência de 35 cobranças:** 30 conferidas (meses em aberto no CTN iguais aos do retrato) e **5 com
  `dado_suspeito` (14%)**. Nas 5, os lançamentos `rejeitada` em aberto passam de `meses_atraso + 1` (de 6 a 11
  contra 3 a 7) e têm buracos.
- **Causa provável (5 de 5):** o número de lançamentos rejeitados **depois** do mês do `ult_mesref` do retrato é
  exatamente o `meses_atraso`. Os demais (de 2 a 8 por pessoa, de até 26 meses atrás) são do mês do `ult_mesref`
  ou de antes, que o retrato não conta como atraso. Cobrar pelo total diria à pessoa que ela deve de 6 a 11
  meses em vez de 3 a 7; por isso o `dado_suspeito` barrava.
- **Mas filtrar sempre quebrou 2 das 30 certas** (2 de 35 no total): numa o filtro zerou tudo e noutra a dívida
  caiu de 3 para 1 mês (valor menor que o do CTN, inseguro). O `ult_mesref` às vezes contradiz os lançamentos.
  **Feito, D55:** o `ult_mesref` só desempata, quando as abertas seriam suspeitas e as posteriores a ele são
  exatamente os meses do retrato. **Pergunta ao dev** (`duvidas.md`, T9.18): o que o `ult_mesref` significa? Sem a
  resposta, o agente não cobra o que fica suspeito (o padrão seguro).

### Pronto quando

- Uma leitura real de 3 matrículas confere nos formatos e nas contagens (nada de dado pessoal no relatório).
- O `agente importar-ctn --simular` mostra os totais esperados e sai com 0.
- A rotina das baixas roda sem erro e a `baixa_ctn` enche.
- O serviço **não sobe** com a chave do CTN e a do Luna sem `AGENTE_LLM_DADOS_REAIS_LIBERADOS`.

## T4 — Disparador

**Precisamos de:** o acesso e a documentação da API, se ela confirma entrega e leitura, e os templates
aprovados na Meta (D28).

**Implementar** `Disparador` (`integracoes/disparador.py`):
- `enviar_template(telefone_e164, template, variaveis, botoes) -> str` devolve o id do envio;
- a falha no envio levanta exceção;
- o template de abertura leva o botão de resposta "Pagar com Pix", com o payload `PAGAR_PIX`.

**Templates usados** (nomes no `politica.toml`):

| Template | Para quê |
|---|---|
| `template_abertura` | a mensagem de abertura, com a dívida e o botão |
| `template_confirmacao` | a confirmação de pagamento fora das 24h (utilidade) |

**A cuidar:** a confirmação por template não confere se o envio já saiu. Se o disparador entrega e o
commit seguinte falha, ela pode sair em dobro. Se a API tiver consulta pelo id do envio, dá para fechar
isso.

## T10 — Jev (sem efeito pela D59)

O Jev saiu do código com o pipeline de leitura, decisão e validação (D59). O Luna faz a leitura, a decisão
e a redação numa chamada só, e o código confere. A integração, o contrato e a trava de dados dele ficam na tag
`antes-do-enxugamento`. A dúvida T10 de [`duvidas.md`](duvidas.md) fica sem efeito.

## T6 — LLM (`gpt-6-luna`)

**Decidido (D42, D59):** o LLM é o `gpt-6-luna`, da OpenAI, com a chave da empresa. Ele lê a conversa, escolhe a ação
e redige o texto numa chamada só; o código confere.

**Precisamos de:** a chave e a confirmação da retenção de dados. O histórico da conversa vai à OpenAI a cada turno,
então é preciso saber se dá para desligar o uso para treino (LGPD).

**Hoje:** o cliente está pronto, `LLMTurnoOpenAI` (`integracoes/llm_turno.py`), no contrato do `LLMTurno`. O Luna só
escreve marcadores (`{valor_final}`), e o backend preenche os números. **Sem a chave, o serviço não sobe.**

**Ligar** (variáveis no [`.env.example`](../.env.example)):

| Variável | Padrão | Para quê |
|---|---|---|
| `AGENTE_LLM_API_KEY` | (obrigatória) | a chave da OpenAI |
| `AGENTE_LLM_URL` | `https://api.openai.com/v1` | a base da API de chat completions |
| `AGENTE_LLM_MODELO` | `gpt-6-luna` | o modelo; trocar exige que o provedor aceite `json_schema` estrito (abaixo) |
| `AGENTE_LLM_CORPO_EXTRA` | `{"reasoning_effort": "none"}` | JSON que entra no corpo de todo pedido |
| `AGENTE_LLM_DADOS_REAIS_LIBERADOS` | `false` | a trava do M2 (ver "A trava dos dados reais"); sem `true`, o serviço não sobe |

**Atenção ao `reasoning_effort`:** o Luna **recusa a temperatura** (HTTP 400, e toda chamada falha) sem o
`reasoning_effort: "none"`; foi medido no outro agente da GeniAI. O padrão já o manda. Se mudar o modelo ou o
provedor, confira o que ele aceita no corpo.

**Atenção ao `json_schema` estrito:** a saída do Luna vai em `response_format` com `json_schema` e `strict: true`
(`integracoes/llm_turno.py`). O modelo ou o provedor novo tem de aceitá-lo e respeitá-lo; senão toda chamada falha e o
turno cai nas 3 tentativas e em `INTEGRACAO_INDISPONIVEL`. Medido em 2026-11-13 com o `gpt-6-luna`: aceitou o esquema
estrito com e sem `OFFER_DISCOUNT` no `enum` da ação (régua com e sem degrau) e a saída validou em todos os turnos do
ensaio local (55/55) e dos 8 roteiros no celular, no `dev2`.

**A confirmar:** a retenção de dados (LGPD) antes de liberar dado real. A comparação que sustentou a escolha do
modelo está em `docs/comparacao-llm.md` da tag `antes-do-enxugamento` (histórico).

## T11 — Transcrição de áudio

**Decidido (D43):** o `gpt-4o-mini-transcribe` transcreve os áudios de até 2 minutos, e o texto segue o
turno normal; o Luna lê a transcrição. É a mesma OpenAI da T6.

**Precisamos de:** a chave da OpenAI para a transcrição e, na T3, o que o Chatwoot manda de verdade
para o áudio do WhatsApp (o outro agente espera `audio/ogg`; o webhook traz o endereço em `data_url`).

**Hoje:** a transcrição está pronta, com a interface `Transcritor` (`integracoes/transcritor.py`), o
`TranscritorOpenAI` e o `TranscritorFalso`. O download do anexo é o `Chatwoot.baixar_anexo`: só da
origem do Chatwoot recebe o token, no máximo 3 redirecionamentos, nunca de https para http, até 5 MB e
com prazo único de 15 s. A falha é um resultado, nunca uma exceção.

**Ligar, só com a chave.** São variáveis próprias, separadas das do LLM. Sem nenhuma, a transcrição fica
desligada e o áudio cai no pedido de texto; com só parte delas, o serviço **não sobe** e diz quais
faltam.

| Variável | Padrão | Para quê |
|---|---|---|
| `AGENTE_TRANSCRICAO_URL` | (sem padrão) | a base da API, por exemplo `https://api.openai.com/v1` |
| `AGENTE_TRANSCRICAO_API_KEY` | (sem padrão) | a chave |
| `AGENTE_TRANSCRICAO_MODELO` | `gpt-4o-mini-transcribe` | o modelo |

**O que o dev precisa saber do turno:**

- o áudio entra no histórico como `[áudio transcrito: "<texto>"]` (o falho, como `[áudio — não foi
  possível ouvir]`, e o longo demais, como `[áudio longo demais — não ouvido]`). O prompt do Luna explica esses
  rótulos: é o que o cliente falou e pode ter erro de transcrição;
- o texto transcrito é dado de cliente e **nunca vai ao log**; o endereço do áudio também não (o `configurar_logging` deixa os loggers `httpx` e `httpcore` em WARNING, porque eles logam cada pedido com o endereço inteiro);
- os limites estão no `politica.toml`: `audio_max_segundos` (120), `audio_max_bytes` (5 MB),
  `audio_prazo_segundos` (15), `audio_sem_duracao_max_bytes` (2 MB, o teto do formato cuja duração não
  se lê, fora do Ogg Opus), `transcricao_prazo_segundos` (15) e `transcricao_max_caracteres` (3000);
- cada transcrição vira uma nota privada na conversa;
- o áudio sem leitura leva o pedido de texto (`pedir_texto_audio_longo` e `pedir_texto_audio_falhou`),
  fora do teto, e na 2ª vez o humano assume, com o motivo `AUDIO`.

**Pronto quando:** o Cauê testa no piloto: o áudio curto, o longo, o "já paguei" falado e a nota no
Chatwoot.

---

## Checklist de entrega

- [ ] T2: o servidor no ar (o `dev2` para o teste), com o Postgres e o túnel
- [ ] T3: o Chatwoot configurado e o webhook recebendo; o `data_url` do áudio com a mesma origem do
  `AGENTE_CHATWOOT_URL`
- [ ] T1: o Pix real passa no `ContratoPix`; o webhook com o formato real (tarefa 6.4)
- [ ] T9: a escrita do CTN real passa no `ContratoCTN`; o webhook com o formato real (tarefa 6.5)
- [ ] T4: o disparador real e os templates aprovados
- [ ] T6: a chave da OpenAI em `AGENTE_LLM_API_KEY`, o Luna aceitando o corpo do pedido e a retenção de dados
  confirmada
- [ ] T11: a chave da transcrição em `AGENTE_TRANSCRICAO_*`, o content-type real do áudio (T3) e o teste no piloto (D43)
