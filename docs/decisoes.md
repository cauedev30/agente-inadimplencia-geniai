# Decisões

Cada decisão tem um número fixo. Uma decisão revisada não é apagada: ela ganha a marca de qual decisão
a revisou.

| # | Data | Decisão |
|---|---|---|
| D1 | 2026-10-15 | Serviço próprio no servidor de produção, ligado ao Chatwoot como **Agent Bot** |
| D2 | 2026-10-15 | A **carteira de inadimplentes é importada no próprio agente** |
| D3 | 2026-10-15 | O agente **dispara a mensagem de abertura** (a que cita a dívida) **pela API do disparador** |
| D4 | 2026-10-15 | *(Revisada pela D22: o Pix vem do banco do Cartão de Todos, não de um PSP à parte.)* **Pix dinâmico** de um PSP com **webhook de confirmação**: um PSP só, recebendo na conta de cada franquia |
| D5 | 2026-10-15 | Toda saída do robô, seja pelo teto ou por gatilho do Jev, manda o cliente para o **canal externo da franquia** |
| D6 | 2026-10-15 | O LLM de redação é **escolhido por teste**. Candidato: Groq `gpt-oss-120b` |
| D7 | 2026-10-19 | **Stack em Python**, mesmo com outros projetos da GeniAI em Go. O Go entra só se surgir gargalo, porque tende a render mais com muitos clientes ao mesmo tempo. Ver a análise abaixo |
| D8 | 2026-10-19 | **Teto de 3 mensagens de resolução** do agente. Na 4ª, o cliente é encaminhado para o canal externo. *(Em 2026-10-21 a operação recomendou cerca de 5; revisada pela D33: teto de 5.)* |
| D9 | 2026-10-19 | Repositório: este, `cauedev30/agente-inadimplencia-geniai` |
| D10 | 2026-10-19 | *(Revisada pela D21.)* Repositório público por enquanto; só o Cauê trabalha nele |
| D11 | 2026-10-19 | *(Revisada pela D16 na parte de fora da janela.)* **Janela de atendimento:** segunda a sexta, das 8h às 18h; sábado, das 8h às 12h. Fora dela, uma mensagem reforçaria o horário |
| D12 | 2026-10-19 | **LGPD:** o uso do Jev com os dados dos clientes está autorizado. Zero Data Retention e No Training ligados em cada chamada |
| D13 | 2026-10-19 | *(Corrigida pela D17.)* Uma franquia só, em teste. Cada franquia terá as suas credenciais, separadas; a divisão em várias franquias fica para quando o produto escalar |
| D14 | 2026-10-19 | O teto de mensagens vale para **todas as franquias**, sem configuração por franquia. *(Era o teto de 3; com a D33, vale para o teto de 5.)* |
| D15 | 2026-10-19 | O repositório fica público por enquanto e vai virar **privado em breve** |
| D16 | 2026-10-19 | **Fora da janela, o agente não responde nada**, porque cada mensagem custa. O que chega fora do horário é respondido quando a janela abre, como se tivesse chegado no horário. Os atendentes também veem essas mensagens no horário deles. **Domingo e feriado ficam fora da janela** |
| D17 | 2026-10-19 | **A primeira franquia é o Cartão de Todos João Pessoa Sul, em uso real**, não teste. Se o projeto der certo, expande para outras |
| D18 | 2026-10-19 | *(Revisada pela D32: o canal externo é o webchat.)* O canal externo é um **web chat ou outro número de WhatsApp** da franquia |
| D19 | 2026-10-19 | *(Com a D33, a mensagem fixa passa a ser a 6ª; o texto continua.)* **A 4ª mensagem é fixa, sem LLM e sem Jev:** *"Para o problema ser resolvido de forma mais eficaz, entre em contato conosco por esse canal de atendimento."*, com o link do canal externo |
| D20 | 2026-10-19 | Sem restrição de dado no repositório enquanto ele for público, porque ele vira privado em breve |
| D21 | 2026-10-19 | O projeto é feito pelo Cauê **junto com outro dev** |
| D22 | 2026-10-20 | **Pix pela API Pix do banco do Cartão de Todos, via integração**, sem PSP à parte. A cobrança é **gerada por cliente**, com identificador (txid), e o banco avisa por **webhook** quando o Pix cai. Pix fixo (a mesma chave ou QR para todos) foi descartado, porque não deixa o agente saber sozinho quem pagou |
| D23 | 2026-10-20 | **Sem parcelamento.** A franquia não parcela a dívida: o pagamento é sempre à vista |
| D24 | 2026-10-20 | **A dívida é a mensalidade (R$ 33,40) vezes o número de mensalidades em aberto**, sem juros nem multa |
| D25 | 2026-10-20 | **A carteira é dividida em réguas** pelo número de mensalidades em aberto, nesta ordem: NR, R1 (1), R2 (2), R3 (3), **Prévia de Desfiliado**, R4 (4), R5 (5) e R6 (6). É a divisão do manual da Adimplência do CDT. A régua define a condição oferecida e a abordagem |
| D26 | 2026-10-20 | **O Pix não vai na mensagem de abertura.** A abertura leva um **botão "Pagar com Pix"**, e o Pix é gerado na hora em que o cliente toca nele. Um Pix que chega sem o cliente pedir parece golpe, e gerar na hora evita criar cobranças que vencem sem uso |
| D27 | 2026-10-21 | **NR é "novo rejeitado":** o cliente que acabou de virar inadimplente |
| D28 | 2026-10-21 | **A aprovação dos templates na Meta não é risco do projeto:** a GeniAI já tem a infraestrutura, muitos templates estão aprovados e a equipe resolve as aprovações. Os templates do manual da Adimplência são exemplo, não modelo a seguir |
| D29 | 2026-10-21 | **O valor da régua é o desconto máximo, não o valor final.** Sem desconto na NR, R1 e R2; até R$ 90 na R3 e na R4; até R$ 75 na R5; até R$ 60 na R6. O agente apresenta **primeiro o débito total** e libera o desconto **aos poucos**, durante a negociação. Se o cliente só desbloqueia o cartão, sem cadastrar um novo, paga o **valor integral**. Fonte: Lohanny (operação). Os degraus do desconto e o valor da Prévia estão na G1.1 |
| D30 | 2026-10-21 | **A troca da forma de pagamento é papel do agente.** Ele manda o **link de pagamento do CTN**, em que o cliente quita a dívida e já cadastra o cartão de crédito ou débito. O Pix todo mês não é uma recorrência saudável. A troca para a **conta de energia (Energisa) é manual** e vai para o humano |
| D31 | 2026-10-21 | **O agente trabalha integrado ao CTN**, o sistema com os clientes, as matrículas e o histórico. A **baixa** do pagamento entra na aba de mensalidades do CTN |
| D32 | 2026-10-21 | **O atendimento humano sai do WhatsApp para um canal externo**, no desenho do webchat ([`ideia-webchat.md`](ideia-webchat.md)). As conversas são distribuídas entre os atendentes em **rodízio igualitário**. Revisa a D18 |
| D33 | 2026-10-22 | **Teto de 5 mensagens de resolução** do agente, igual em todas as franquias (D14). A **6ª mensagem é fixa, sem LLM e sem Jev**: a **confirmação de pagamento**, se o cliente pagou, ou o encaminhamento para o canal externo (texto da D19), se não pagou. A confirmação **não consome** o teto e sai sempre, mesmo depois da 6ª. A **mensagem de abertura** (template, D3) **não conta** no teto. Revisa a D8 e responde a C2 e a G2.1 |
| D34 | 2026-10-22 | **O desconto vale só no link do CTN, com o cadastro do cartão** (D30). No **Pix**, o cliente paga o **valor integral**. Responde, pelo Cauê, a pergunta da G1.1 sobre o Pix sem cartão |
| D35 | 2026-10-22 | Até a G1.1 fechar os degraus, o desconto sai em **3 degraus iguais** até o máximo da régua (D29). Na R3: R$ 30, R$ 60 e R$ 90. Cada objeção de valor libera o degrau seguinte. É um **parâmetro por régua**, que muda sem mexer no código |
| D36 | 2026-10-22 | **Propensão ao cartão.** O backend calcula, pelo histórico de pagamentos do CTN, se o cliente tende a cadastrar o cartão. Exemplo: 3 meses seguidos pagando Pix no mesmo dia do mês, que já é uma recorrência. **Propenso:** o link do CTN vai primeiro, e a confirmação de pagamento já convida a cadastrar o cartão. **Não propenso:** o Pix vai primeiro, e o link com desconto só entra se ele pedir desconto. A regra é um parâmetro (número de meses, tolerância de dias). Sem histórico, o cliente conta como não propenso |
| D37 | 2026-10-22 | **Uma mensagem pode levar mais de um assunto**, porque cada mensagem custa. Exemplo: a confirmação do Pix e, na mesma mensagem, o convite para cadastrar o cartão; ou o débito total junto com o link. O Jev 2 escolhe **a ação principal**; o **código anexa** as que vão junto, por uma tabela fixa ([`arquitetura.md`](arquitetura.md), seção 4). O LLM redige uma mensagem só |
| D38 | 2026-10-22 | **Saídas do turno ativo, aprovadas pelo Cauê.** O desconto barrado (sem objeção ou com os degraus esgotados) vai para o **humano**. Quando o Jev 2 decide NONE, o agente **não responde**. O **EM_RISCO** tem 1 turno para melhorar; sem melhora, humano. Se o Jev 3 reprova o texto duas vezes, sai uma **resposta segura** (uma vez por conversa); na segunda vez, humano. O LLM, o CTN ou o Pix fora do ar: **3 tentativas**, depois humano. Responde a C4 e o caso dos degraus esgotados da G1.1 |
| D39 | 2026-10-22 | **Pagamento em dobro vai para o humano.** Se o cliente paga duas vezes a mesma cobrança (o Pix e o link, ou o Pix vencido e o novo), o 2º pagamento fica registrado como pago, sem nova baixa e sem nova mensagem, e a conversa ganha uma **nota privada e a etiqueta** no Chatwoot para o atendente cuidar da devolução |
| D40 | 2026-10-22 | **O toque no botão "Pagar com Pix" é respondido na hora, mesmo fora da janela.** É a exceção à D16: o cliente que toca quer pagar agora, e a resposta é uma mensagem só, que conta no teto (D26) |
| D41 | 2026-10-25 | **O áudio do cliente é transcrito pelo LLM**, e o texto segue o turno normal: o Jev 1 lê a transcrição como leria a mensagem escrita. O Jev continua lendo só texto. Responde a T11 e substitui o padrão de pedir a mensagem em texto. Ler imagem (vision) ainda está em análise pelo Cauê |

## Análise do D7: Go × Python

Tempo de um turno, de ponta a ponta:

| Etapa | Tempo |
|---|---|
| Webhook do Chatwoot chegar | ~1 min (dado do desenho) |
| Espera para juntar mensagens em rajada | ~7 s |
| 3 chamadas ao Jev | 0,2 a 1,5 s (dado do desenho) |
| LLM redigindo | ~0,5 a 2 s (estimativa) |
| PSP gerando o Pix | ~0,3 a 1 s (estimativa) |
| Código do agente (regras, cálculo, banco) | Go: poucos ms; Python: dezenas de ms |

A linguagem pesa menos de 1% do turno, porque quase todo o tempo é espera de serviço externo. O volume
também não pesa: mesmo numa campanha grande, o pico fica em poucos turnos por segundo.

- **A favor do Go:**
  - o Tech Lead conhece a linguagem;
  - outro serviço da GeniAI já resolve em Go parte da infraestrutura (Agent Bot, dedupe, rajada, fila,
    janela de horário);
  - o deploy fica igual ao dos outros serviços.
- **A favor do Python:**
  - a fluência de quem desenvolve;
  - a calibração dos limiares do Jev, que é análise de dados.

Decisão: **Python** (D7). O Go fica como saída se aparecer gargalo.
