# Dúvidas em aberto

Atualizado em 2026-10-22. São as perguntas que faltam para fechar a spec. Elas estão separadas por quem
responde: **G** para o gestor da franquia, **T** para o time técnico e **C** para o Cauê.

Contexto rápido: o agente cobra pelo WhatsApp os clientes inadimplentes do **Cartão de Todos João
Pessoa Sul**. Ele tenta resolver o pagamento em até 5 mensagens; a 6ª é fixa: confirma o pagamento ou
manda o cliente para o canal externo da franquia (D33). O robô só fala de segunda a sexta, das 8h às 18h, e no sábado, das 8h às 12h. Mais
em [`spec.md`](spec.md).

Quando uma dúvida for respondida, ela vira decisão em [`decisoes.md`](decisoes.md) e ganha aqui a marca
**Respondida em AAAA-MM-DD → Dxx**.

Enquanto não há resposta, a dúvida entra no código como parâmetro, com o **padrão** marcado aqui
(a lista completa está na seção 4 de [`spec.md`](spec.md)).

---

## Para o gestor (negócio)

### G1. Política de negociação

É a dúvida mais importante: sem ela, o agente não sabe o que pode oferecer ao cliente.

1. A franquia **dá desconto** na dívida? Até quantos %? Sobre a dívida inteira ou só sobre juros e
   multa? **Respondida em 2026-10-21 → D29:** é o **desconto máximo**, liberado aos poucos. Continua
   aberto:
   - **Os degraus:** quanto liberar em cada passo e quando. Por exemplo, na R3: R$ 30, depois R$ 60,
     depois R$ 90? Quem decide o degrau é o Jev, então a regra tem de ser fixa. **Padrão (D35):** 3
     degraus iguais até o máximo.
   - **A Prévia e a R6:** o manual dava R$ 75 na R6 e R$ 60 na Prévia; a operação disse R$ 60 na R6 e não
     falou da Prévia. **Padrão:** R$ 60 nas duas.
   - **Os números:** como desconto máximo, a R3 (R$ 100,20) pode pagar só R$ 10,20, e a R6 (R$ 200,40)
     paga no mínimo R$ 140,40. O desconto encolhe justo nas réguas altas. É isso mesmo?
   - ~~**O desconto depende do cartão?** Quem só desbloqueia paga o valor integral (D29). Quem paga no
     Pix, sem cadastrar o cartão, também paga o integral?~~ **Respondida pelo Cauê em 2026-10-22 →
     D34:** sim; o desconto só vale no link do CTN, com o cartão. O gestor pode rever.
   - **Mais de 6 mensalidades:** qual é a regra? **Padrão:** a condição da R6.
   - **Objeção de valor nas réguas sem desconto (NR, R1, R2):** o que o agente faz? **Padrão:** explica
     a condição; na 2ª objeção, a conversa vai para o humano.
   - **Degraus esgotados:** o cliente objeta de novo depois do último degrau (a 4ª objeção na R3). O
     que o agente faz? **Padrão (2026-10-22):** a conversa vai para o humano, como na NR. A outra saída
     seria repetir a última oferta.
2. ~~**Aceita parcelar?** Em quantas vezes, no máximo? Tem valor mínimo de parcela?~~ **Respondida em
   2026-10-20 → D23:** não tem parcelamento.
3. ~~**A regra muda** conforme o tempo de atraso ou o valor da dívida? Por exemplo: até 30 dias de
   atraso, uma condição; acima de 90, outra.~~ **Respondida em 2026-10-20 → D25:** muda conforme a
   régua, que segue o número de mensalidades em aberto.
4. ~~Como são calculados **juros e multa** de atraso?~~ **Respondida em 2026-10-20 → D24:** não há juros
   nem multa; a dívida é a mensalidade vezes o número de mensalidades em aberto.
5. A política **fica fixa ou muda com frequência**? Se muda, quem avisa a mudança e como?

### G2. O que acontece quando o cliente paga

1. Depois que o Pix cai, o cliente recebe **uma mensagem de confirmação**? Lembrando que cada mensagem
   custa na Meta.
   - **Posição do Cauê (2026-10-20):** a confirmação é essencial e deve ser enviada mesmo que passe do
     teto de 3 mensagens (D8).
   - ~~**Em aberto:** essa confirmação conta no teto ou fica fora dele?~~ **Respondida em 2026-10-22 →
     D33:** fica fora do teto de 5; ela é a 6ª mensagem fixa quando o cliente pagou.
2. ~~**Quem dá baixa na dívida:** o agente, a franquia no sistema dela, ou os dois?~~ **Respondida em
   2026-10-21 → D31:** a baixa entra na aba de mensalidades do CTN, com o agente integrado a ele. Fica
   para o dev: como a integração escreve a baixa (T9).
3. A franquia quer ser **avisada de cada pagamento**? Por onde: e-mail, WhatsApp, relatório?
4. **E se o cliente recebe o Pix e não paga?** Por quanto tempo o Pix vale? Depois disso, o agente volta
   a cobrar ou o caso vai para o atendente? **Padrão:** o Pix vale 24h, e o agente não volta a procurar
   o cliente; se ele escrever de novo, a conversa segue dentro do teto.

### G3. Feriados

Domingo e feriado ficam fora do horário do robô. Quais feriados contam: só os nacionais, ou também os
da Paraíba e os de João Pessoa?

Em 2026-10-21 a operação confirmou o horário comercial, das 8h às 18h, sem falar do **sábado**. O
sábado das 8h às 12h (D11) continua?

### G4. Como medir se deu certo

1. O que conta como sucesso: a **taxa de clientes que pagaram**, o **número médio de mensagens até o
   pagamento**, o **custo por cobrança** ou outra medida?
2. Qual é o número que conta como "deu certo"? Por exemplo: pagamento em X% dos casos, ou custo abaixo
   de R$ Y por cobrança.

### G5. Carteira de inadimplentes

1. **De onde vem a lista** de inadimplentes: planilha, sistema da franquia, export do Cartão de Todos?
   **Em parte (2026-10-21):** o CTN tem os clientes, as matrículas e o histórico (D31), então a lista
   provavelmente sai dele. Falta saber se ela já vem com a régua.
2. **Quais informações** ela tem? Nome, telefone, valor, vencimento, dias de atraso, número do contrato,
   CPF?
3. **Com que frequência** ela é atualizada: diária, semanal, a cada campanha?

### G6. Mensagem de abertura

Qual é o **texto da primeira mensagem**, a que avisa o cliente da dívida? Ela precisa ser aprovada pela
Meta como template antes de ser usada.

**Respondida em 2026-10-21 → D26 e D28:** a abertura leva o botão "Pagar com Pix", e não o Pix. A
aprovação na Meta não é risco, e os templates do manual são só exemplo: o texto e os botões da abertura
são desenhados pelo projeto.

### G7. Canal externo do João Pessoa Sul

~~Para onde a 4ª mensagem manda o cliente: qual é o **link do web chat** ou o **número de WhatsApp** da
franquia?~~ **Respondida em 2026-10-21 → D32:** o atendimento humano vai para o **webchat**
([`ideia-webchat.md`](ideia-webchat.md)), com rodízio igualitário entre os atendentes. Continuam
abertas:

1. Tem **atendente de plantão** na janela, para responder o webchat em tempo real?
2. A **confirmação pelos 3 primeiros dígitos do CPF** antes de mostrar a dívida é aceitável?
3. Em qual **domínio** a página fica: da franquia, do Cartão de Todos ou da GeniAI?

### G8. Boleto

Só o **Pix** basta, ou a franquia também quer oferecer **boleto**? Hoje o boleto não está previsto, e o
caso iria para o atendente.

### G9. Troca da forma de pagamento

A Adimplência do CDT não só cobra: ela também tenta tirar o cliente do Pix mês a mês e passá-lo para o
**pagamento recorrente** (cartão de crédito, débito ou conta de energia). O caminho principal é o
**link do CTM**, gerado pela matrícula, em que o cliente paga e já cadastra o cartão. O Cauê marcou esta
dúvida como importante em 2026-10-20.

Segundo o Cauê (2026-10-20), o cartão é um dos trabalhos da implantação; falta confirmar com o gestor.

1. ~~O agente **também faz a troca**, mandando o link do CTM, ou **só cobra** pelo Pix e encerra?~~
   **Respondida em 2026-10-21 → D30:** faz a troca, pelo link de pagamento do CTN. A conta de energia
   (Energisa) é manual e vai para o humano.
2. Na troca, o manual da Adimplência cobra junto **o mês que está para vencer**. Por exemplo: o cliente
   deve agosto e setembro vence na semana seguinte; na troca, paga os dois. O agente faz igual?
   **Padrão:** não cobra junto; o link leva só a dívida.
3. O link do CTM serve **só para cadastrar o cartão, sem pagar**? Isso resolveria o caso de quem já
   pagou por Pix. A alternativa sugerida pela operação, um formulário com os dados do cartão, não deve
   ser usada: coletar o código de segurança fora de um gateway certificado fere o PCI-DSS.

### G10. Prévia de Desfiliado

É a régua do último aviso antes de o cliente perder o cartão (D25). Fica entre a R3 e a R4.

1. **O que coloca o cliente na Prévia?** As outras réguas seguem o número de mensalidades em aberto,
   mas o manual não diz o critério da Prévia: dias de atraso, uma data do sistema ou outro.
2. **A que horas sai a mensagem?** O manual dispara às 17h20, mas a janela do agente fecha às 18h
   (D11, D16). Sobram 40 minutos para conversar; quem responder depois das 18h só recebe resposta no
   dia seguinte, às 8h. Sugestão: antecipar para o bloco das 16h30 ou antes.

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


**Antes de ligar a chave em produção (2026-10-22):** o histórico da conversa vai para o Groq a cada
turno. Falta confirmar a retenção de dados do Groq (LGPD) e se dá para desligar o uso para treino.

### T7. Entrada em fases

O desenho prevê banco → modo sombra → Jev 3 → Jev 1 → Jev 2 (ver [`arquitetura.md`](arquitetura.md),
seção 8). **Vale essa ordem** neste projeto? O modo sombra roda com clientes reais do João Pessoa Sul?

**Padrão (2026-10-22):** a sombra roda nas conversas reais do João Pessoa Sul, só lendo, **também
enquanto um atendente responde**: o turno grava os laudos e o que o agente teria feito, marcado como
"humano na conversa", para comparar com o que o atendente fez. Os laudos do Jev falso ficam marcados
como tal e não entram na calibração.

### T8. As 6 ações do Jev 2

**Respondida em 2026-10-22 → D34 a D37:** sai o `SIMULATE`; entram o `OFFER_DISCOUNT` e o
`SEND_CTN_LINK`, e o código anexa as ações que vão junto na mesma mensagem. Ver
[`arquitetura.md`](arquitetura.md), seção 4.

As ações `INFORM_DEBT`, `SIMULATE`, `GENERATE_PIX`, `OUT_OF_SCOPE`, `NONE` e `HUMAN` **cobrem a política
do João Pessoa Sul**, ou falta alguma (desconto à vista, por exemplo)? Depende da G1. Sem parcelamento
(D23), o `SIMULATE` e a pergunta `pediu_parcelamento` do Jev 1 perdem o uso como estão.

### T9. CTM

Pelo que o Cauê sabe em 2026-10-20, o **CTM** é o sistema de registro do Cartão de Todos, um histórico
grande de tudo, e **tem API para consulta** ("varredura"). Ainda não se sabe se o agente vai trabalhar
direto nele.

1. O que o agente **consulta no CTM**? Matrícula, régua, débitos, se o cliente já pagou? A carteira
   (G5) pode vir dessa varredura?
2. O **link de pagamento** (G9) pode ser gerado pela API, a partir da matrícula? Há webhook quando o
   cliente paga por ele?
3. O manual da Adimplência fala em **"CTN"** e a avaliação da operação fala em **"CTM"**. São o mesmo
   sistema? **Em parte (2026-10-21):** na reunião, a operação falou em **CTN**; "CTM" deve ser o mesmo
   sistema, escrito errado. Confirmar.
4. O CTN expõe o **histórico de pagamentos com a forma e a data** de cada um? A propensão ao cartão
   (D36) é calculada com ele. **Padrão:** 3 meses seguidos de Pix, com tolerância de 3 dias no dia do
   mês.
5. O **link do CTN aceita um valor com desconto**, definido pelo agente (D34, D35)?
6. Quem paga pelo link **já sai com a baixa no próprio CTN**, ou o agente também registra a baixa?

### T10. Jev a partir do Python

O acesso ao Jev é pela Vercel AI Gateway, com o `experimental_evaluate` do AI SDK 7, que é TypeScript
(arquitetura, seção 9). O serviço é em Python (D7). **Como o Python chama o Jev:** por HTTP direto ao
gateway, se ele expuser o endpoint de avaliação, ou por um serviço mínimo em Node ao lado? Até
responder, o Jev entra como interface falsa (arquitetura, seção 10).

### T11. Transcrição de áudio

O cliente pode mandar áudio de até 2 min (arquitetura, seção 1), mas o Jev lê texto. **Qual serviço
transcreve?** **Padrão até lá:** o agente responde pedindo a mensagem em texto.

---

## Para o Cauê

### C1. Conferir D1 a D6

As decisões D1 a D6 foram recuperadas de uma sessão de 2026-10-15/16. Conferir se estão como você
decidiu.

### C2. Teto de mensagens

**Respondida em 2026-10-22 → D33:** teto de 5; a 6ª é fixa (confirmação de pagamento ou canal externo).

Em 2026-10-21 a operação achou o teto de 3 + 1 (D8, D19) muito restrito e recomendou **cerca de 5
mensagens**. Os casos simples, como a NR, fecham rápido. As réguas altas pedem mais negociação ou chegam
com atrito com a clínica, e esses casos devem ir logo para o humano.

- Pesa a favor de rever: o desconto liberado aos poucos (D29) dificilmente cabe em 3 mensagens.
- Meio-termo possível: um **teto por régua** (3 nas baixas, 5 nas altas), mantendo a saída rápida para
  o humano quando houver atrito. A D14 continua valendo: o teto seria o mesmo em todas as franquias.
- Depende disso a G2.1: a confirmação de pagamento conta no teto?

~~Até você decidir, vale a D8.~~

### C3. Turno adiado que passa das 24h do WhatsApp

A mensagem que chega fora da janela é respondida quando a janela abre (D16). Uma mensagem de sábado
às 12h30 só seria respondida na segunda às 8h, e aí a janela de 24h do WhatsApp já fechou: só pode
sair um template. **Padrão até você decidir:** um template de retomada pelo disparador, fora do teto.

Já existe um caso parecido: a resposta do agente que ficou pronta e não saiu antes de fechar a janela de
24h é descartada hoje, sem template (2026-10-22). Entra na mesma decisão.

### C4. Comportamentos do turno ativo

A etapa 5 precisou de regras que a spec não fixava. **Padrões até você decidir (2026-10-22):**

1. **O Jev 2 decide NONE:** o agente não responde nada naquele turno.
2. **EM_RISCO:** a conversa entra em risco com o tom impaciente ou quando o cliente repete sem avançar.
   Se o turno seguinte não melhora, vai para o humano. Em risco, só o texto para de propor; o código
   ainda manda o desconto, o link ou o Pix se o Jev 2 escolher. Barrar as propostas é uma linha.
3. **O Jev 3 reprova o texto duas vezes:** sai uma resposta segura, curta e sem números, uma vez por
   conversa; na segunda vez, a conversa vai para o humano.
4. **O LLM fora do ar:** a conversa vai para o humano.
5. **O CTN, o Pix ou o Chatwoot falhando:** 3 tentativas, com espera que dobra; depois, o humano.
6. **Erro no próprio código:** 3 tentativas; depois, o humano, com o erro no log (decidido por você em
   2026-10-22).

