# Depois da linha de chegada

Tudo o que ficou de fora do enxugamento de 2026-11-11 (D59, D60, D61). **Regra:** o que não serve à linha de chegada
(o teste do Luciano e da Lohanny no `dev2`) vira uma linha aqui, sem código; só sobe de lista se bloquear o teste
(o testador vê o defeito, ou o servidor expõe segredo ou dado). Nada se apaga: o item resolvido ganha "feito" e o commit.

Origem: a auditoria externa de 2026-11-10 (commit `5dd7b9f`, achados `AUD-NN`) e as pendências abertas do projeto. A
auditoria original e a triagem completa estão no workspace (`fontes/2026-11-10-auditoria-externa.md` e
`specs/2026-11-11-triagem-da-auditoria.md`). Para voltar a qualquer parte cortada: tag `antes-do-enxugamento`.

Destinos: **AGORA** (tarefa do plano `specs/planos/2026-11-11-enxugamento.md`), **CORTE** (some com a D59, D60 ou D61),
**ANTES DO PILOTO REAL** (cliente real, depois do Pix T1, do link T9 e do disparador T4), **ANTES DE ESCALAR**,
**TESTE MEDE** (sem código até o teste mostrar) e **NÃO SERVE**.

## 1. Os 42 achados da auditoria

Onde a auditoria deu teste, o teste vai junto
com a correção quando a hora chegar (texto completo na fonte).

| Id | Caso (resumo) | Correção sugerida | Destino |
|---|---|---|---|
| AUD-01 | O segredo de `/webhooks/chatwoot/{segredo}` vai em claro ao log de acesso (`logs.py:13`) | Regex que corta também essa rota; trocar o segredo em uso | AGORA, 1.5 |
| AUD-02 | Atendente que abre a cobrança (JP Sul) grava `human_owned` e cala o robô; a sombra não mostra porque `barrar_humano` só vale no ativo | Gravar `human_owned` só depois de mensagem do cliente | A parte da sombra: CORTE (1.4). A regra do atendente que abre primeiro: ANTES DO PILOTO REAL (no teste ninguém abre) |
| AUD-03 | Um timeout do Luna/Jev vira handoff na hora, antes das 3 tentativas prometidas | Deixar a exceção subir enquanto `falhas_turno < integracao_tentativas` | AGORA, 1.7 (a parte do Jev: CORTE) |
| AUD-04 | Mensagem de sábado 13h passa de 24h na segunda; `Retomar` manda o template ao `DisparadorFalso` e consome a rajada | Handoff com nota enquanto o disparador for falso | AGORA, 1.6 (motivo `FORA_DAS_24H`) |
| AUD-05 | `travar_conversas_e_cobranca` não relê a cobrança: baixa e confirmação em dobro (precisa prova no Postgres) | `.all()` na consulta ou `refresh(with_for_update)` | ANTES DO PILOTO REAL |
| AUD-06 | Linha de baixa malformada (valor negativo) trava a rotina das baixas por 24h | `_baixas_validas` em `_baixas_da_pessoa`; parar só em falha de transporte | ANTES DO PILOTO REAL |
| AUD-07 | Comprovante em foto/PDF sem legenda chega vazio ao LLM; cliente que pagou fica sem resposta | Gate para rajada só com anexo → `CONCILIACAO`; rótulos `[imagem enviada]` | AGORA, 1.7; **feito** (`c1a1545`, D63): a foto que chega como `[Image]` com anexo vai à conciliação; só a figurinha fica sem resposta |
| AUD-08 | Fixa e notas do handoff saem dentro da transação que pode ser repetida (fixa paga até 3 vezes) | Gravar PENDENTE e despachar depois do commit | ANTES DO PILOTO REAL |
| AUD-09 | Telefone casa com 2 cobranças ativas: vence a de menor id (expõe dívida a terceiro) | Nenhuma ligada; handoff com nota | ANTES DO PILOTO REAL |
| AUD-10 | `uma_chamada`: sem conferência de promessa ("pode pagar em duas vezes"), `OFFER_DISCOUNT` sem objeção, `contar_objecao` nunca chamado | (1) desconto só após INFORM_DEBT; (2) `objecao_valor`; (3) conferir `reply` com perguntas de segurança | TESTE MEDE (se a Lohanny vir desconto cedo ou promessa falsa, voltam as 3 partes) |
| AUD-11 | Desconto em R$ fixo pode passar de 90% da dívida (R3: R$ 90 de R$ 100,20) | `desconto_max_percentual_da_divida` | TESTE MEDE: pergunta à Lohanny (padrão G); depois ANTES DO PILOTO REAL |
| AUD-12 | Dinheiro a devolver sem aviso: boleto + Pix pagos; Pix antigo (3 meses) pago depois da dívida cair | Atalho só para `LINK_CTN`; exigir valor igual; nota ao atendente | ANTES DO PILOTO REAL |
| AUD-13 | Baixas podem se perder (`MARGEM_DE_RELEITURA` 0; fuso; linhas puladas sem rastro) — PROVÁVEL | Margem de 3h; `em_aberto == []` como `None`; gravar id das puladas | ANTES DO PILOTO REAL (depende dos ciclos reais) |
| AUD-14 | Cobrança com link pendente nunca é encerrada pelo import | Contar só Pix pendente e não vencido, ou expirar links | ANTES DO PILOTO REAL |
| AUD-15 | Leitura longa do CTN que falha gasta o orçamento de 1h | Gravar `_ultima_leitura_longa` só após sucesso | ANTES DO PILOTO REAL |
| AUD-16 | Uma thread faz tudo; fila sem trava; `/saude` não vê a thread morta | `skip_locked`, pool, heartbeat no `/saude`, advisory lock | AGORA só o `/saude` (2.1); o resto ANTES DE ESCALAR |
| AUD-17 | Humano é beco sem saída; toque no Pix perde para o teto | Pix fixo; toque antes do teto; liberar toque em motivos não sensíveis; alerta de cliente sem resposta | Pix fixo: AGORA (1.2). O resto: ANTES DO PILOTO REAL |
| AUD-18 | Tom sem limiar; EM_RISCO gasto em silêncio | Limiar para o tom; `NONE` mantém o estado | CORTE (era do pipeline) |
| AUD-19 | Mensagem descartada deixa degrau/forma/pagamento no banco | `_descartar` restaura o degrau e expira o pagamento | ANTES DO PILOTO REAL |
| AUD-20 | Dois motores com regras que divergem (terceiro, objeção, tom, palavra-chave) | Um motor só | CORTE (D59) |
| AUD-21 | Dado pessoal escapa da máscara e vai cru à OpenAI/TypeSafe; `hide_parameters=False` | `hide_parameters=True`; `Formatter` mascarado; mascarar histórico antes de chamada externa | ANTES DO PILOTO REAL |
| AUD-22 | Sem retenção nem apagamento (LGPD) | `agente expurgar --dias N`; `agente apagar-titular` | ANTES DO PILOTO REAL |
| AUD-23 | 3 dígitos do CPF no webchat caem em ~14 dias | Contador vitalício de bloqueios | CORTE (webchat, D60) |
| AUD-24 | `.dockerignore` sem `pre-sombra/`; imagem roda como root; migração em todo start; SQLite no ativo | `.dockerignore`, `USER app`, migração como release, recusar SQLite no ativo | AGORA só `.dockerignore` e usuário sem root (2.1); o resto ANTES DE ESCALAR |
| AUD-25 | Não há CI | `.github/workflows/ci.yml` com Postgres | NÃO SERVE agora (a suíte roda em toda tarefa) |
| AUD-26 | Webhooks leem corpo sem limite (DoS) | Reusar o leitor com teto de 64 KB | ANTES DO PILOTO REAL |
| AUD-27 | Injeção de prompt pelo histórico e pela transcrição | Histórico em JSON; escapar aspas da transcrição; rotular a nota | A parte do redator: CORTE. A do `uma_chamada` e da transcrição: ANTES DO PILOTO REAL |
| AUD-28 | Sinais julgados sobre o histórico inteiro (conversa devolvida refaz handoff) — PROVÁVEL | Marcar mensagens novas da rajada | ANTES DO PILOTO REAL |
| AUD-29 | Webhook de pagamento sem replay; formato provisório | Refazer com o esquema real (T1, T9) | ANTES DO PILOTO REAL |
| AUD-30 | Redirects do download de áudio podem ir a host interno | Recusar IP privado/link-local | ANTES DO PILOTO REAL |
| AUD-31 | Telefone pessoal real em `tests/ferramentas/test_ensaio_demo.py:196-197`; repo já foi público | Trocar por número fictício | AGORA, 1.7 |
| AUD-32 | `/docs` e `/openapi.json` públicos; CSP só com `frame-ancestors`; `identifier_hash` permanente | Desligar docs; CSP; HMAC obrigatório | AGORA só o `/docs` (2.1); a parte do webchat: CORTE; o resto ANTES DE ESCALAR |
| AUD-33 | Jev 3 recebe centavos e converte | Passar valores já em reais | CORTE |
| AUD-34 | Evento autenticado sem `conversation`/`id` dá 500 | `.get` e devolver "ignorado" | AGORA, 1.7 |
| AUD-35 | Mensagem entre a leitura das pendentes e o `flush` fica sem turno | Varredura de conversa com mensagem não consumida e `turno_em` nulo | ANTES DO PILOTO REAL |
| AUD-36 | Disparo confere a janela uma vez só | Conferir por envio | ANTES DO PILOTO REAL |
| AUD-37 | `limiares_jev2` sem validação de completude | Validar chaves ao carregar | CORTE |
| AUD-38 | `registrar_mensagem` engole todo `IntegrityError`; `_gravar_envio` ignora `None` | Engolir só a violação do dedupe | ANTES DO PILOTO REAL |
| AUD-39 | Baixa com `matricula` de formato diferente é descartada em silêncio | Normalizar e comparar, ou levantar erro | ANTES DO PILOTO REAL |
| AUD-40 | Valor divergente sem conversa no Chatwoot não avisa ninguém | Contar em `/saude/pagamentos` | ANTES DO PILOTO REAL |
| AUD-41 | `_status_mudou` reescreve o estado em qualquer evento "pending" | Conferir `humano_na_conversa` antes | ANTES DO PILOTO REAL |
| AUD-42 | Código morto: `EM_NEGOCIACAO`, `MotivoHumano.BOLETO`, ramo inalcançável de `_rodar_turno`, `_entregue`, `_historico` duplicado, `processar_sem_acao` como padrão, regras de redação duplicadas, `intencao_pagar`/`entendeu_proposta` | Apagar junto com o motor | Parte em 1.6 (EM_NEGOCIACAO, BOLETO, ramo, `_entregue`) e 1.3 (histórico, redação, perguntas); `processar_sem_acao` como padrão: ANTES DE ESCALAR |

## 2. Arquitetura-alvo (seção 6 da auditoria)

| Item | Destino |
|---|---|
| A. Sombra = ativo sem efeitos | Sem objeto: a sombra sai (D61) |
| B. Um motor | Feito pela D59 (1.3) |
| C. Regras como dados (`RegraAcao`, `RegraMotivo`) | ANTES DE ESCALAR |
| D. Matriz de casos como fonte de verdade (YAML) | A decidir depois do teste (o cardápio de 5 casos já é um começo) |
| E. Estado explícito (`TRANSICOES`) | ANTES DE ESCALAR |
| F. Uma função `fechar_cobranca` | ANTES DE ESCALAR (as emendas AUD-05, 12, 13 nascem nela) |
| G. Todo efeito externo depois do commit | ANTES DO PILOTO REAL (é o AUD-08) |
| H. `agente relatorio --dias N` | ANTES DO PILOTO REAL: com ele o piloto é julgado |
| I. Worker (trava, pool, heartbeat, advisory lock) | Heartbeat já em 2.1; o resto ANTES DE ESCALAR |

## 3. Outras pendências técnicas

- Ciclos do atraso das baixas, em horário comercial, quando houver cota (decide `MARGEM_DE_RELEITURA`; roteiro em
  `specs/arquivo/2026-11-10-proximo-passo-conferencia-do-conjunto.md`).
- Menores da D55: "só encolhe" só vale com `conferida_em` de hoje; cobrança reativada no mesmo dia ignora mês novo até o
  dia seguinte; desempate do `ult_mesref` errado vale o dia todo.
- Menores da D54: `dt_baixa` só com a data + Pix à noite → aviso de dobro espúrio; `meses_atraso` diferente das
  rejeitadas → nunca QUITADA (`conferir.py:186`); baixa malformada no topo do cursor volta a cada ciclo.
- Achados da demo: A20 ("quais meses?"), A22 (valor proposto pelo cliente ignorado), A23 (link novo a cada pedido), A24
  (silêncio depois do handoff), A2 ("Oi" sem resposta; o 1º contato de teste já resolve para testadores).
- Risco do `importar-ctn`: encerra ATIVAS da mesma campanha que saíram do retrato (teto de 25%); rodar só em banco que
  não misture origens.
- Aceito para a T3: atendente em conversa de venda cria `Conversa` (`chatwoot_webhook.py`).
- Pendências antigas em `specs/arquivo/2026-11-10-pendencias-fora-do-caminho.md` (Etapa 11 do Jev real perde o objeto pela D59;
  redatores; PARADA 8; menores esperando sistema real).
- Pontos que a outra sessão deixou para a PARADA 2 antiga (checklist do plano antigo): "Oi" antes do `/paguei <n>`, a linha
  "[TESTE] No cliente real…" da abertura no histórico do Luna, a ordem na tela do `/paguei`.
- Postgres nunca rodou com o modo teste (cobre a tarefa 2.2). A trava com duas conexões (AUD-05) fica para depois.
- `git stash@{0}` no `repo/` guarda a 2.5 interrompida (`ensaiar.py`, `subir.py`, 2 testes, `politica-teste-ensaio.toml`):
  ponto de partida da tarefa 1.8.
- Frentes com o gestor e com o dev (G1.1, G4, G2.4, G9, G7, G5, G3, T1, T2, T3, T4, T9): continuam em
  `repo/docs/duvidas.md`; as de webchat (G7) ficam "sem efeito pela D60".
- Achados da tarefa 1.3 (código sem leitor depois do Jev): `politica.objecoes_sem_desconto_ate_humano` e a coluna
  `Conversa.objecoes_sem_desconto`; `proximo_estado(..., em_risco)` chamado sempre com `False`; motivos
  `JEV_INDISPONIVEL`, `DECISAO_JEV2`, `BAIXA_CONFIANCA`, `SEM_MELHORA` e `OBJECAO_SEM_DESCONTO` e
  `EstadoConversa.EM_RISCO` sem uso; o `LLMTurnoOpenAI` do serviço não é fechado no encerramento (já era assim).
- Achados da tarefa 1.4/1.6: `Dependencias.disparador` sem leitor no turno (só `pagamento/concluir.py` usa um, com
  dependências próprias); `pagamento/concluir.py` envia a confirmação do Pix sem checar o piloto (hoje só recebe quem teve
  Pix gerado pelo agente, ou seja, o piloto; lembrar no piloto real).
- Achado da tarefa 1.5: uma linha antiga de `pagamento` com a pendência `RESOLVER_WEBCHAT` em `pendencias` ficaria sem passo
  (`_PASSOS` perdeu a chave); o erro é capturado e logado. Só afeta banco com dado antigo; o serviço ainda não está hospedado.
- Efeito da 1.5 a lembrar: a fixa do golpe (`HUMANO_NO_WHATSAPP`) só sai no motivo `DESCONFIANCA`; com outro motivo e
  `desconfia_golpe` sim, a 6ª é a `CANAL_EXTERNO` comum (como o plano manda).
- Achados da tarefa 1.7/1.8: `ensaiar.py` ainda tem resquícios do Jev (`jev2_acao`, `jev2_p`, `reprovadas`, leitura de
  laudos); `roteiros/demo.yaml` e `v1-operacao.yaml` são da era do Jev e os `texto_tem` de Pix e CTN podem não bater mais; a
  docstring do `ensaiar.py` ainda cita `demo.yaml`; `MotivoHumano.REDATOR_INDISPONIVEL` sem uso (enum mantido).
- Escolha da 1.7: a rajada só com foto/arquivo vai ao humano; com áudio junto, o turno segue para transcrever.
- **Banco bloqueado no ensaio** (revisão da PARADA 1, 2026-11-11, causa verificada nos `servico.log` dos ensaios 0306, 0940
  e 0948): os 500 `database is locked` (11 no total) são todos `INSERT INTO cliente`, a corrida do 1º contato entre dois
  POSTs simultâneos (`chatwoot_webhook.py` ~170-174 → `casos.py` ~139-158): o perdedor leu dentro da transação e tenta
  escrever, e o SQLite devolve `OperationalError` na hora, sem esperar o timeout; `except IntegrityError` não pega. A
  mensagem fica gravada uma vez. No Postgres vira `IntegrityError` no índice único de `matricula_ctn` e o tratamento A8
  cobre (a confirmar na 2.2). A falha do caso-3 na 1ª rodada foi de rede até o Luna (`getaddrinfo failed`, 2 vezes), não
  do banco; a correção do ensaio (pular turno ERRO) saiu no lote da PARADA 1.
- **O turno segura a trava de escrita durante a chamada ao Luna** (`orquestrador.py` ~1032-1052, `fila.py` ~73,
  `historico.py` ~64): no Postgres o UPDATE da `conversa` trava a linha, e o `agendar_turno` do webhook
  (`chatwoot_webhook.py` ~256 → `fila.py` ~41) espera o commit do turno (de ~8 s a ~30 s no pior caso; não há
  `lock_timeout`). O webhook só demora, não dá 500 nem duplica turno (`chatwoot_message_id` único). **A conferir na 2.4:**
  quanto o Chatwoot espera a resposta do webhook (suspeita: 5 s) e o que faz quando o Agent Bot passa do prazo. Não é
  regressão; o código da tag fazia igual.
- **Achados DEPOIS da revisão da PARADA 1** (2026-11-11):
  - (a) `_limpar_retorno` compara com um `turno_em` velho (`orquestrador.py` ~960-963, `expire_on_commit=False` em
    `db/sessao.py` ~36): se o cliente escreve durante o turno, o agendamento do webhook é trocado por `agora` e corta a
    rajada de 7 s, e numa janela de milissegundos troca por `None` (mensagem sem turno; parte do AUD-35). Correção
    mínima: `sessao.refresh(conversa, ["turno_em"])` antes de comparar; mexe em estado.
  - (b) Dois eventos tortos que dão 500, do tipo do AUD-34: `chatwoot_webhook.py` ~183 (`int(dados.get("id", 0))` com
    `"id": null`) e ~78 (`int(conversa["id"])` com id não numérico). Correção mínima: ler o id com `.get` e devolver
    "ignorado".
  - (c) Toque no "Pagar com Pix" fora da janela, com o teto atingido ou a cobrança quitada, manda o handoff com mensagem
    fora do horário (`turno/gates.py` ~153-165 → `orquestrador.py` ~1160-1179): a exceção da D40 vale para o Pix, não
    para o handoff; já era assim antes do enxugamento; mexe em envio.
  - (d) Resquício de nome: `ConfigRedator` (`config.py` ~38) hoje só guarda o Luna.
  - (e) 1º contato com dois POSTs simultâneos no SQLite responde 500 (já descrito no item do banco bloqueado; o ensaio
    aceita um 200 só). (f) Resíduo possível do ensaio: na nova tentativa que dá certo depois de um turno de ERRO
    (AUD-03), `erros_no_log` pode achar a linha `ERROR` da tentativa anterior (`log.exception("o turno da conversa ... falhou")`,
    depois do byte de início do passo) e marcar o passo como ERRO. Só se aparecer no ensaio do `dev2`.
- Achados da tarefa 1.9 (documentos): o serviço só sobe com `AGENTE_LLM_DADOS_REAIS_LIBERADOS=true` (trava M2 sempre ativa;
  **decidido em 2026-11-11 pelo Cauê:** no `dev2`, `true` só para o teste, com os 3 testadores e casos fictícios; **conferir a retenção da OpenAI no painel (T6: treino e dias de guarda) é exigência ANTES DO PILOTO REAL**, quando o CTN real entrar); fora do piloto o webhook ainda grava a mensagem (só o
  turno é ignorado) e mensagem de atendente fora do piloto marca `human_owned`; D38 diverge do código (vale a D47); resquícios
  de nome do Jev (`PERGUNTAS_JEV1`, `LeituraJev1`, `BatchJev`, comentário do webhook, `ConfigPolitica` cita `agente simular`);
  (o `/saude` já enxerga o laço parado desde a 2.1).
- **O token do Agent Bot não lê mensagens (401 no Chatwoot real, achado do 1º teste no `dev2`, 2026-11-11; corrigido em `6379d91`):**
  `ChatwootHttp.mensagens_do_agente` devolve `[]` em 401/403 e o envio segue ("não consegui conferir, manda"). Sem leitura, a
  conferência de duplicata fica sem lastro: (a) no reenvio (`orquestrador.py` `_despachar` ~402), se a mensagem chegou ao cliente e
  a resposta do Chatwoot se perdeu (timeout depois da entrega), ela sai de novo; (b) em `_resolver_pendente` (~813) a mensagem
  pendente é descartada sem conferência e o teto pode contar uma a menos. Janelas raras. A saída é o token do bot passar a ler as
  mensagens (Chatwoot, T3) ou guardar o id da resposta do envio. O ensaio não pega (o Chatwoot falso não tem essa permissão).
- **Leitura de imagem (futuro, importante; D62, 2026-11-11):** a foto e a figurinha chegam ao agente como `[Image]` ou `[Sticker]` + anexo.
  Implementar: (1) **feito** (`c1a1545`, D63): o texto-padrão do gateway com anexo conta como "sem texto" e a foto vai ao humano
  (`CONCILIACAO`, o desenho original do AUD-07); só a figurinha fica sem resposta; (2) ler o comprovante (imagem ou PDF) com um modelo de visão, confirmando valor e data antes de
  conciliar. Os anexos já guardam o tipo e o endereço (`mensagem.anexos`); o token do Agent Bot baixa a mídia? *a conferir* (o áudio já usa `baixar_anexo`).
- **Achados (B) da revisão Opus da Etapa 2 (PARADA 2, 2026-11-11), nada bloqueia o teste:** (1) `deploy/dev2/agente.env.exemplo` traz
  `AGENTE_LLM_DADOS_REAIS_LIBERADOS=false` e o comentário "token novo, depois da rotação", que contradizem a decisão de 2026-11-11
  (`true` no teste): quem copiar o exemplo sobe um serviço que não arranca. (2) LEIAME: o comando dos testes do Postgres põe a senha na
  linha de comando (aparece no `ps` e no histórico); "Subir" não avisa que, sem o `politica-teste.toml`, o Docker cria uma pasta root no
  lugar; "Atualizar o código" não apaga o que saiu do repositório. (3) `compose.yaml`: log `json-file` sem limite e sem `healthcheck`
  (o 503 do `/saude` não aciona nada). (4) O túnel rápido que reinicia sozinho para o webhook em silêncio: o Cauê confere uma vez por dia
  durante o teste. (5) `Dockerfile`: o `chown -R /app` duplica a camada do `.venv` (`COPY --chown`). (6) `configurar_chatwoot.py`: `KeyError`
  sem `bot_id` no JSON; aspas no valor do `agente.env` iriam para a URL. (7) Aviso: quem está no grupo `docker` do `dev2` lê o `agente.env`
  por `docker inspect`, apesar do `chmod 600`.

## Do lote "fechar o agente" (2026-11-13)

- **Cortesia com pergunta de confirmação:** a lista descarta a pontuação, então "certo?", "ok, certo?" e "entendi, certo?"
  ficam em silêncio quando o Luna devolve `NONE`. Pode ser uma pergunta que fica sem resposta. Medir no teste; se
  aparecer, a mensagem com "?" sai da cortesia. (Conferência curta da PARADA.)
- **Ponto final grudado no código Pix:** na resposta do `INFORM_DEBT`, o Luna fecha a frase com "." logo depois do
  `{pix_copia_e_cola}` ("…copia e cola: [código]."). Com o Pix real, o cliente pode copiar o ponto junto e o pagamento
  não passa. O botão "Pagar com Pix" já põe o código numa linha sozinho. **ANTES DO PILOTO REAL** (com o T1).
  (Celular, 2026-11-13.)
- **Ordem do "[TESTE] Simulando a confirmação" e do "Obrigado":** depois do `/paguei`, as duas saem no mesmo segundo e
  a ordem varia. Só existe no modo teste. **NÃO SERVE**. (Celular, 2026-11-13.)

## Da leitura a frio (2026-11-13)

Fonte: a leitura a frio de `77a78f2` (`C:/aud-frio/LEITURA-A-FRIO.md`). O lote de correção levou os invariantes I-1,
I-2, I-3, I-4 e I-8, o F-1 e os documentos D-1 a D-12. O que não entrou:

- **I-5, figurinha junto de áudio não ouvido** (`turno/transcricao.py:68-73`): o anexo da figurinha e o rótulo
  `[Sticker]` tiram a rajada do pedido de texto; com `NONE`, sai o handoff `SEM_RESPOSTA` em vez de "Não consegui ouvir
  seu áudio". Lado seguro (vai ao atendente). **NÃO SERVE**.
- **I-6, `reply` com mais de 1000 caracteres** (`turno/uma_chamada.py:357`, contra `:329` e `:547-551`): a saída inteira
  não valida e vira falha de integração; o sinal (ex.: `alega_pagamento`) chega ao atendente só depois das 3
  tentativas, com `INTEGRACAO_INDISPONIVEL`. **TESTE MEDE** (se o Luna escrever textos assim, tirar o limite do esquema
  e deixar o conferidor reprovar).
- **I-7, mensagem só de símbolo vale como cortesia** (`turno/catalogo.py:94-95` e `:135`): "❓", "🤔", "👎" e "😡" ficam
  em silêncio com `NONE`. Dentro da letra da política; parente da cortesia com pergunta. **TESTE MEDE**.
- **F-2, docstring de `motivos_do_texto`** (`turno/redacao.py:151`): ainda cita o texto do handoff, que ninguém confere
  mais. **NÃO SERVE**.
- **F-3, teste que não testa** (`tests/turno/test_uma_chamada_resolver.py:150-154`): o `NONE` com texto só de espaços
  repete o da linha 141; falta o caso do `NONE` com texto de verdade (sai o handoff `SEM_RESPOSTA`). **NÃO SERVE**.
- **F-4, constante sem uso** (`tests/turno/auxiliares.py:29`, `REPLY_NONE`, um uso em
  `tests/turno/test_orquestrador_uma_chamada.py:349`). **NÃO SERVE**.
- **F-5, asserção que não falha** (`tests/turno/test_uma_chamada_resolver.py:207`, `"erro" not in dados`: o campo saiu
  da `ResolucaoTurno`). **NÃO SERVE**.
- **F-6, teste que confere a palavra errada** (`tests/turno/test_uma_chamada_entrada.py:290`, `"terceiro" in texto`: a
  regra de redação do terceiro saiu do prompt). **NÃO SERVE**.
- **F-7, cobertura dos gates novos** (`tests/turno/test_transcricao.py:131-153`): em `tests/turno/test_gates.py` faltam
  figurinha + foto, figurinha + áudio (I-5) e figurinha fora da janela; o lote do I-4 já pôs o rótulo com e sem anexo.
  **NÃO SERVE**.
- **F-8, figurinha e rótulo duplicados** (`turno/catalogo.py:109-110`, `turno/gates.py:95`, `turno/transcricao.py:33`):
  `_FIGURINHA` em dois módulos e `_ROTULO_DA_IMAGEM` copiando o rótulo de `ROTULOS_DE_ANEXO`. Mudar um rótulo quebra o
  silêncio da figurinha sem teste que acuse (o lado seguro é o atendente). **ANTES DE ESCALAR**.
- **F-9, docstring do módulo dos gates** (`turno/gates.py:3-7`, contra `:169-178`): diz "humano na conversa, reinício";
  o código faz o reinício antes do humano. **NÃO SERVE**.
- **F-10, linha do prompt quase sem efeito** (`turno/uma_chamada.py:295`): "leve a preocupação a sério" está em "como
  escrever o reply", mas com `desconfia_golpe` ou a palavra "golpe" o `reply` nunca sai. Mexer no prompt só com dado
  novo. **NÃO SERVE**.
- **F-11, defesas de antes do esquema estrito** (`turno/uma_chamada.py:359-364`, `integracoes/llm_turno.py:19-21`): o
  `null` que vira `""` e o JSON cercado por crases não têm entrada possível com o `json_schema` estrito. Ficam se o
  provedor puder mudar. **ANTES DE ESCALAR**.
- **F-12, teste da palavra-chave sem conferir o Luna** (`tests/turno/test_orquestrador_uma_chamada.py:157`): dá uma
  resposta ao Luna falso que nunca é usada e não confere `pedidos == []`. **NÃO SERVE**.
