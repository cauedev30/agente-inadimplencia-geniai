# Dúvidas em aberto

Atualizado em 2026-10-20. São as perguntas que faltam para fechar a spec. Elas estão separadas por quem
responde: **G** para o gestor da franquia, **T** para o time técnico e **C** para o Cauê.

Contexto rápido: o agente cobra pelo WhatsApp os clientes inadimplentes do **Cartão de Todos João
Pessoa Sul**. Ele tenta resolver o pagamento em até 3 mensagens; na 4ª, manda o cliente para o canal
externo da franquia. O robô só fala de segunda a sexta, das 8h às 18h, e no sábado, das 8h às 12h. Mais
em [`spec.md`](spec.md).

Quando uma dúvida for respondida, ela vira decisão em [`decisoes.md`](decisoes.md) e ganha aqui a marca
**Respondida em AAAA-MM-DD → Dxx**.

---

## Para o gestor (negócio)

### G1. Política de negociação

É a dúvida mais importante: sem ela, o agente não sabe o que pode oferecer ao cliente.

1. A franquia **dá desconto** na dívida? Até quantos %? Sobre a dívida inteira ou só sobre juros e
   multa?
2. ~~**Aceita parcelar?** Em quantas vezes, no máximo? Tem valor mínimo de parcela?~~ **Respondida em
   2026-10-20 → D23:** não tem parcelamento.
3. **A regra muda** conforme o tempo de atraso ou o valor da dívida? Por exemplo: até 30 dias de
   atraso, uma condição; acima de 90, outra.
4. Como são calculados **juros e multa** de atraso?
5. A política **fica fixa ou muda com frequência**? Se muda, quem avisa a mudança e como?

### G2. O que acontece quando o cliente paga

1. Depois que o Pix cai, o cliente recebe **uma mensagem de confirmação**? Lembrando que cada mensagem
   custa na Meta.
2. **Quem dá baixa na dívida:** o agente, a franquia no sistema dela, ou os dois?
3. A franquia quer ser **avisada de cada pagamento**? Por onde: e-mail, WhatsApp, relatório?
4. **E se o cliente recebe o Pix e não paga?** Por quanto tempo o Pix vale? Depois disso, o agente volta
   a cobrar ou o caso vai para o atendente?

### G3. Feriados

Domingo e feriado ficam fora do horário do robô. Quais feriados contam: só os nacionais, ou também os
da Paraíba e os de João Pessoa?

### G4. Como medir se deu certo

1. O que conta como sucesso: a **taxa de clientes que pagaram**, o **número médio de mensagens até o
   pagamento**, o **custo por cobrança** ou outra medida?
2. Qual é o número que conta como "deu certo"? Por exemplo: pagamento em X% dos casos, ou custo abaixo
   de R$ Y por cobrança.

### G5. Carteira de inadimplentes

1. **De onde vem a lista** de inadimplentes: planilha, sistema da franquia, export do Cartão de Todos?
2. **Quais informações** ela tem? Nome, telefone, valor, vencimento, dias de atraso, número do contrato,
   CPF?
3. **Com que frequência** ela é atualizada: diária, semanal, a cada campanha?

### G6. Mensagem de abertura

Qual é o **texto da primeira mensagem**, a que avisa o cliente da dívida? Ela precisa ser aprovada pela
Meta como template antes de ser usada.

### G7. Canal externo do João Pessoa Sul

> ⭐ **Ideia em avaliação, com prioridade alta (2026-10-20): o atendimento humano vai para um webchat**,
> com o cliente no widget do Chatwoot e o atendente no Chatwoot, em vez de outro WhatsApp. Pontos
> positivos, negativos, soluções e as perguntas para o gestor em [`ideia-webchat.md`](ideia-webchat.md).

Para onde a 4ª mensagem manda o cliente: qual é o **link do web chat** ou o **número de WhatsApp** da
franquia?

### G8. Boleto

Só o **Pix** basta, ou a franquia também quer oferecer **boleto**? Hoje o boleto não está previsto, e o
caso iria para o atendente.

---

## Para o time técnico

### T1. PSP do Pix

**Respondida em parte em 2026-10-20 → D22:** Pix pela API Pix do banco do Cartão de Todos, com cobrança
por cliente (txid) e webhook. Continuam abertas as perguntas 1, 2 e 4 abaixo.

Qual PSP gera o Pix dinâmico com webhook de confirmação (D4)? Candidatos: Asaas, Efí (Gerencianet),
Mercado Pago ou o banco que a franquia já usa. Hoje basta receber na conta do João Pessoa Sul. Se o
projeto expandir, o PSP precisa atender várias franquias, cada uma com a sua conta, numa integração só.

**Indicação de 2026-10-20 (provável, ainda não decidido):** em conversa com o dev, a tendência é usar
o **Pix do próprio Cartão de Todos, pelo banco deles, via integração**, sem um PSP à parte. Para fechar,
falta responder:

1. **Qual banco?**
2. **Confirmar** que o banco tem **API Pix** com **cobrança com identificador (txid)** e **webhook de Pix
   recebido**. A D22 depende disso.
3. ~~É um Pix fixo de verdade ou uma cobrança gerada por cliente?~~ **Respondida → D22:** cobrança
   gerada por cliente.
4. O dinheiro cai na conta **da franquia João Pessoa Sul** ou na do **Cartão de Todos (franqueadora)**?
   A D4 previa a conta de cada franquia.

### T2. Servidor de produção

Em qual servidor o serviço roda (D1), e quem é o responsável por ele?

### T3. Chatwoot do João Pessoa Sul

Qual **conta e qual inbox** do Chatwoot o agente atende como Agent Bot? Quem cria o Agent Bot e passa o
token?

### T4. API do disparador

O agente dispara a abertura pela API do disparador (D3). Já temos **acesso e documentação** dessa API?
Ela confirma entrega e leitura?

### T5. Contas e chaves

Quem cria e paga as contas de cada serviço: **Vercel AI Gateway** (Jev), **Groq** (LLM) e o **PSP**?

### T6. Escolha do LLM

O LLM de redação é escolhido por teste (D6). **Como é esse teste**: quais conversas, quais critérios? E
quem aprova o resultado?

### T7. Entrada em fases

O desenho prevê banco → modo sombra → Jev 3 → Jev 1 → Jev 2 (ver [`arquitetura.md`](arquitetura.md),
seção 8). **Vale essa ordem** neste projeto? O modo sombra roda com clientes reais do João Pessoa Sul?

### T8. As 6 ações do Jev 2

As ações `INFORM_DEBT`, `SIMULATE`, `GENERATE_PIX`, `OUT_OF_SCOPE`, `NONE` e `HUMAN` **cobrem a política
do João Pessoa Sul**, ou falta alguma (desconto à vista, por exemplo)? Depende da G1. Sem parcelamento
(D23), o `SIMULATE` e a pergunta `pediu_parcelamento` do Jev 1 perdem o uso como estão.

---

## Para o Cauê

### C1. Conferir D1 a D6

As decisões D1 a D6 foram recuperadas de uma sessão de 2026-10-15/16. Conferir se estão como você
decidiu.
