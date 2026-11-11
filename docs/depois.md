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
| AUD-07 | Comprovante em foto/PDF sem legenda chega vazio ao LLM; cliente que pagou fica sem resposta | Gate para rajada só com anexo → `CONCILIACAO`; rótulos `[imagem enviada]` | AGORA, 1.7 |
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
