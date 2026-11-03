# Guia de integração: ligar os sistemas reais

A v1 está pronta em código (etapas 1 a 8), mas roda com **implementações falsas** dos sistemas de fora.
Este guia diz, para cada um, **o que precisamos receber**, **onde entra no código** e **como saber que
ficou pronto**. As perguntas originais estão em [`duvidas.md`](duvidas.md) (T1 a T11).

## Como cada integração funciona

Todo sistema de fora entra por uma **interface** em `src/agente/integracoes/` (um `Protocol` do Python).
Cada interface tem uma implementação falsa, usada nos testes e no desenvolvimento, e um **teste de
contrato** em `tests/integracoes/`. A implementação real:

1. implementa os mesmos métodos da interface;
2. herda a classe de contrato (`ContratoPix`, `ContratoCTN`, `ContratoJev`) num arquivo de teste e
   define as fixtures que o contrato pede, contra o sandbox ou o ambiente de teste;
3. levanta a exceção de "fora do ar" da interface quando o sistema não responde (rede caída, prazo
   estourado ou 5xx). O turno trata essa exceção: tenta de novo e, depois de `integracao_tentativas`
   (no `politica.toml`), passa a conversa para o humano;
4. é ligada em `montar_dependencias` (`src/agente/api/principal.py`), lida das variáveis de ambiente.
   Sem as variáveis, o serviço continua com a falsa.

Credenciais só por variável de ambiente (`.env`, fora do git). O [`.env.example`](../.env.example)
traz os nomes, sem os valores. O repositório é público: nenhum dado real de cliente em teste ou log.

## Resumo

| # | Sistema | Precisamos de | Arquivo novo | Bloqueia |
|---|---|---|---|---|
| T2 | Servidor | Dokku, Postgres, domínio | — (deploy pronto) | subir em sombra |
| T3 | Chatwoot do JP Sul | conta, inbox, Agent Bot, token | — (cliente pronto) | subir em sombra |
| T1 | Pix do banco do CDT | API, sandbox, webhook | `integracoes/pix_<banco>.py` | tarefa 6.4 |
| T9 | CTN | API, ambiente de teste, webhook | `integracoes/ctn_http.py` | tarefa 6.5 |
| T4 | Disparador | API, templates aprovados | `integracoes/disparador_http.py` | abertura real |
| T10 | Jev | como chamar do Python | `integracoes/jev_<forma>.py` | modo ativo |
| T6 | LLM (`gpt-6-luna`, D42) | chave, retenção de dados | — (`RedatorOpenAI` pronto) | redação real |
| T11 | Transcrição de áudio | chave da OpenAI (D43) | — (`TranscritorOpenAI` pronto) | áudio do cliente |

A ordem que destrava mais: **T2 e T3** (o agente sobe em sombra no JP Sul, só lendo), depois **T10**
(sem o Jev real o modo ativo é recusado), depois **T1, T9 e T4** (pagamento e abertura reais).

---

## T2 — Servidor

**Precisamos de:** o servidor (Dokku, pela D1), um Postgres e o domínio público do serviço, que o
Chatwoot e os webhooks vão chamar.

**Já pronto:** o `Dockerfile` roda `alembic upgrade head` e sobe o uvicorn. Ver a seção "Deploy" do
[`README`](../README.md).

- `AGENTE_DATABASE_URL=postgresql+psycopg://...`
- `AGENTE_MODO=sombra` (o ativo é recusado até a T10)
- o health check do Dokku consulta `GET /saude`, que não toca no banco. `GET /saude/pagamentos` e
  `GET /saude/webchat` contam o que ficou pendente.

**Pronto quando:** o serviço sobe, as migrações rodam até a `0011` e o `/saude` responde.

**Testar no Postgres** (a suíte roda em SQLite): as travas da confirmação de pagamento e do turno
(`FOR NO KEY UPDATE`, na ordem conversa → cobrança → pagamento) só foram conferidas pelo SQL emitido.
Falta um teste com duas conexões ao mesmo tempo. A importação da carteira e o disparador seguem a mesma
ordem sem travar a conversa (ver o docstring de cada um); a expiração dos Pix trava os pagamentos em
ordem de id.

## T3 — Chatwoot do João Pessoa Sul

**Precisamos de:**
- a URL do Chatwoot, o número da conta e a inbox do WhatsApp que o agente atende;
- o Agent Bot criado nessa inbox, com o token;
- o time dos atendentes humanos (o handoff atribui a ele);
- para o webchat: a inbox **Website**, o token do site e a chave HMAC da validação de identidade.

**Variáveis:** `AGENTE_CHATWOOT_URL`, `AGENTE_CHATWOOT_CONTA`, `AGENTE_CHATWOOT_INBOX`,
`AGENTE_CHATWOOT_TOKEN`, `AGENTE_CHATWOOT_TIME_HUMANO`, `AGENTE_WEBHOOK_SEGREDO`, e as do webchat
(`AGENTE_WEBCHAT_*`).

**Configurar no Chatwoot:**
- o webhook do Agent Bot em `POST /webhooks/chatwoot/{AGENTE_WEBHOOK_SEGREDO}`;
- na inbox Website, **obrigar** a validação de identidade ("Enforce User Identity Validation"). Sem
  ela, a identidade do cliente se forja;
- o rodízio dos atendentes da inbox Website (D32). Detalhes na seção "Configuração do Chatwoot" do
  [`README`](../README.md).

**Já pronto:** o `ChatwootHttp` (`integracoes/chatwoot.py`) e o webhook.

**A confirmar com o Chatwoot real:**
- o formato do toque no botão "Pagar com Pix": hoje o gate lê `content_attributes` com o payload
  `PAGAR_PIX` e também o texto do botão;
- o formato do evento `conversation_created` da inbox Website e o snippet do widget.
- o `data_url` do áudio tem a mesma origem (esquema, host e porta) do `AGENTE_CHATWOOT_URL`: o
  `FRONTEND_URL` do Chatwoot manda o endereço público, e com o `AGENTE_CHATWOOT_URL` interno todo áudio
  vira pedido de texto (o log avisa, sem o link).

**Pronto quando:** uma mensagem do cliente no WhatsApp chega ao webhook e o turno em sombra grava os
laudos.

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

**Webchat:** a página confere os primeiros dígitos do CPF pelo `consultar_cliente`. Com o `CTNFalso`,
que não tem clientes, ela responde 503.

**Pronto quando:** o histórico, o link com desconto e a baixa funcionam numa matrícula de teste.

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
| `template_retomada` | o turno adiado que passou das 24h do WhatsApp (C3) |
| `template_confirmacao` | a confirmação de pagamento fora das 24h (utilidade) |

**A cuidar:** a confirmação por template não confere se o envio já saiu. Se o disparador entrega e o
commit seguinte falha, ela pode sair em dobro. Se a API tiver consulta pelo id do envio, dá para fechar
isso.

## T10 — Jev a partir do Python

**Como chamar (2026-11-03):** a empresa tem **conta direta na TypeSafe, com recarga** (antes o plano usava a
Vercel AI Gateway). O Python chama a API por HTTP, sem serviço em Node:

```bash
curl https://api.typesafe.ai/v1/systemone -H "Authorization: Bearer $AGENTE_JEV_API_KEY"   -H "Content-Type: application/json"   -d '{"model": "jev-latest", "state": {...},
       "questions": {"x": {"type": "noul", "instructions": "..."}}}'
```

- Perguntas: `noul` (só `instructions`; devolve uma probabilidade), `choice` (`criteria` = objeto
  nome da opção → descrição de quando ela se aplica; devolve `choice`, `probabilities` e `confidence`) e
  `score` (`criteria` = lista ordenada com a descrição de cada nível, do menor para o maior; a posição é o
  nível). Na `Pergunta` do agente: `bool` → `noul`, `escolha` → `choice`, `score` → `score`; o campo
  `criterios` traz as descrições na mesma ordem de `opcoes` (sem ele, a descrição de cada opção é o
  próprio nome dela).
- **Prazo total da chamada:** o `timeout` do `httpx` vale por fase (conexão, escrita, leitura, pool), e
  um servidor que manda um pedaço a cada instante o mantém vivo além dele. Por isso o `JevTypeSafe` impõe
  um prazo TOTAL (75% do `jev_timeout_ms`, o `timeout` do construtor): confere o status antes de ler o
  corpo e lê por pedaços, conferindo o relógio a cada um; passou do prazo, levanta `JevIndisponivel`
  ("a TypeSafe passou do prazo") e fecha a resposta. O `timeout` por fase fica igual ao prazo. Nenhuma
  mensagem de erro leva o `state`, o corpo da resposta nem a chave.
- `GET /v1/models` lista `jev-latest` (= `jev-1.14.0`) e `jev-preview`. O padrão é o nome fixo `jev-1.14.0`
  (`AGENTE_JEV_MODELO`), que a API aceita (testado em 2026-11-03, mesma resposta do `jev-latest`): assim os
  limiares não mudam com uma troca silenciosa do `latest`. Subir de versão é decisão e pede recalibrar.
- Testado em 2026-11-03 só com texto inventado: ~300 ms, ~400 tokens de entrada para 3 perguntas, saída
  grátis. Fonte: <https://docs.typesafe.ai/api>.
- A chave fica em `secrets/typesafe.local.json` e entra pela `AGENTE_JEV_API_KEY` (passou pelo chat em
  2026-11-03: **rotacionar** depois da etapa 11).
- **ZDR e No Training pendentes (D12):** a documentação direta não fala deles. Até a TypeSafe confirmar
  por escrito, **só texto inventado** vai ao Jev real; a trava é `AGENTE_JEV_DADOS_REAIS_LIBERADOS`
  (padrão `false`): com a chave e sem a liberação o serviço não sobe e o `agente reproduzir` sai com
  código 2; o `agente simular` não precisa dela. Dúvida T10 em `duvidas.md`.
- Desenho completo e o mapeamento do retorno para o `Laudo`: `specs/2026-11-03-etapa-11-jev-typesafe.md`.

**Implementar** `JevTypeSafe` (`integracoes/jev_typesafe.py`, contrato do `Jev`):
- `origem`: `typesafe:<modelo>`. Ele separa os laudos da falsa na calibração;
- `avaliar(perguntas, estado) -> list[Laudo]`: um laudo por pergunta, na ordem, com o valor dentro das
  opções;
- a falha levanta `JevIndisponivel`.

O prazo (`jev_timeout_ms`) e a conferência dos laudos já são aplicados pelo `JevComPrazo`; o Jev real
não precisa repetir. A semântica da probabilidade de cada laudo precisa ser conferida com a do
`ContratoJev`.

O `JevComPrazo` não espera a chamada atrasada: ela segue numa thread daemon, sem segurar o desligamento
do processo. Por isso o Jev real tem um prazo total próprio, menor que o `jev_timeout_ms` (veja
"Prazo total da chamada", acima), para a chamada não ficar viva depois do turno. No encerramento do
serviço, o `JevComPrazo.fechar()` fecha o cliente HTTP do Jev real.

**Teste:** herdar `ContratoJev` (`tests/integracoes/test_jev.py`).

**Ligar:** em `montar_dependencias`, com a chave e a liberação, usar `JevComPrazo(JevTypeSafe(...))`; sem
a chave, o `JevFalso()`. A recusa do modo ativo fica até a calibração: hoje ele é recusado em produção
enquanto o Jev for o falso.

**Pronto quando:** o Jev real passa no contrato, e as conversas simuladas (`agente simular
tests/conversas`) rodam com ele.

## T6 — LLM (`gpt-6-luna`)

**Decidido (D42):** o LLM é o `gpt-6-luna`, da OpenAI, com a chave da empresa.

**Precisamos de:** a chave e a confirmação da retenção de dados. O histórico da conversa vai à OpenAI a
cada turno, então é preciso saber se dá para desligar o uso para treino (LGPD).

**Hoje:** o cliente está pronto: `RedatorOpenAI` (`integracoes/redator.py`), no contrato do `Redator`.
O LLM só escreve marcadores (`{valor_final}`), e o backend preenche os números. Sem a chave, o serviço
usa o redator falso.

**Ligar, só com a chave** (variáveis no [`.env.example`](../.env.example)):

| Variável | Padrão | Para quê |
|---|---|---|
| `AGENTE_LLM_API_KEY` | (vazio: redator falso) | a chave da OpenAI |
| `AGENTE_LLM_URL` | `https://api.openai.com/v1` | a base da API de chat completions |
| `AGENTE_LLM_MODELO` | `gpt-6-luna` | o modelo; trocar é só mudar isto |
| `AGENTE_LLM_CORPO_EXTRA` | `{"reasoning_effort": "none"}` | JSON que entra no corpo de todo pedido |

**Atenção ao `reasoning_effort`:** o Luna **recusa a temperatura** (HTTP 400, e toda chamada falha)
sem o `reasoning_effort: "none"`; foi medido no outro agente da GeniAI. O padrão já o manda. Se mudar o
modelo ou o provedor, confira o que ele aceita no corpo.

**A confirmar com a chave:** se o Luna aceita a `temperatura = 0.3` que o redator manda (no outro
agente ele aceitou 0). Se recusar, muda o `temperatura` do `RedatorOpenAI` (hoje 0.3, parâmetro do
construtor). Confirmar também a retenção de dados (LGPD) antes de ligar.

**Comparar candidatos:** `agente comparar-redatores tests/conversas --modelo gpt-6-luna:0.10:0.50`
(o `:entrada:saida` é o preço em US$ por milhão de tokens).

## T11 — Transcrição de áudio

**Decidido (D43):** o `gpt-4o-mini-transcribe` transcreve os áudios de até 2 minutos, e o texto segue o
turno normal; o Jev lê a transcrição. É a mesma chave da OpenAI da T6.

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
  possível ouvir]`, e o longo demais, como `[áudio longo demais — não ouvido]`). O **prompt do Jev real
  (T10) deve explicar esses rótulos**: é o que o cliente falou e pode ter erro de transcrição;
- o texto transcrito é dado de cliente e **nunca vai ao log**; o endereço do áudio também não (o `configurar_logging` deixa os loggers `httpx` e `httpcore` em WARNING, porque eles logam cada pedido com o endereço inteiro);
- os limites estão no `politica.toml`: `audio_max_segundos` (120), `audio_max_bytes` (5 MB),
  `audio_prazo_segundos` (15), `audio_sem_duracao_max_bytes` (2 MB, o teto do formato cuja duração não
  se lê, fora do Ogg Opus), `transcricao_prazo_segundos` (15) e `transcricao_max_caracteres` (3000);
- cada transcrição vira uma nota privada na conversa (`nota_transcricao_em_sombra = true` a mantém
  também em sombra);
- o áudio sem leitura leva o pedido de texto (`pedir_texto_audio_longo` e `pedir_texto_audio_falhou`),
  fora do teto, e na 2ª vez o humano assume, com o motivo `AUDIO`.

**Pronto quando:** o Cauê testa no piloto: o áudio curto, o longo, o "já paguei" falado e a nota no
Chatwoot.

---

## Checklist de entrega

- [ ] T2: o servidor no ar, com o Postgres e o domínio
- [ ] T3: o Chatwoot configurado e o webhook recebendo; validação de identidade obrigatória no webchat;
  o `data_url` do áudio com a mesma origem do `AGENTE_CHATWOOT_URL`
- [ ] Sombra no JP Sul rodando (tarefa 3.7)
- [ ] T10: o Jev real passa no `ContratoJev`
- [ ] T1: o Pix real passa no `ContratoPix`; o webhook com o formato real (tarefa 6.4)
- [ ] T9: o CTN real passa no `ContratoCTN`; o webhook com o formato real (tarefa 6.5)
- [ ] T4: o disparador real e os templates aprovados
- [ ] T6: a chave da OpenAI em `AGENTE_LLM_API_KEY`, a temperatura aceita pelo Luna e a retenção de dados confirmada
- [ ] T11: a chave da transcrição em `AGENTE_TRANSCRICAO_*`, o content-type real do áudio (T3) e o teste no piloto (D43)
