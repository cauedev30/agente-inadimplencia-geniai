# Spec do produto

Status: fechada para começar a implementação, com as lacunas marcadas. Atualizado em 2026-10-22.

As decisões citadas (D1, D2, …) estão em [`decisoes.md`](decisoes.md). O que ainda falta definir está
em [`duvidas.md`](duvidas.md). Enquanto uma dúvida não tem resposta, ela entra no código como
**parâmetro** (com um valor padrão) ou como **interface falsa** (o sistema de fora é simulado), e a
implementação não para por ela.

## 1. O problema

As franquias do Cartão de Todos usam o disparador em massa da GeniAI, que roda pela API oficial do
WhatsApp (Meta), para cobrar clientes inadimplentes. Hoje a negociação que vem depois do disparo é
feita por atendentes humanos.

Desde 1º de outubro de 2026, a Meta passou a cobrar também as respostas enviadas dentro da janela de
24 horas, que antes eram de graça. O preço é o da mensagem de utilidade do país, e cada número tem
1.000 respostas grátis por mês. Fontes:
[Landbot](https://landbot.io/blog/whatsapp-business-api-pricing-change-october) e
[Telnyx](https://telnyx.com/resources/whatsapp-business-api-cost), consultadas em 2026-10-15. Falta
confirmar no Billing Hub de um cliente.

Com isso, **cada mensagem de resposta custa dinheiro**. O número de mensagens até o pagamento passa a
ser uma métrica de custo, e não só de experiência do cliente.

## 2. O que o agente faz

- Substitui o atendimento humano na cobrança. A meta é ser mais assertivo que o humano.
- Tenta resolver o pagamento no menor número de mensagens. Se o cliente já chega disposto, uma
  mensagem só pode bastar. **Uma mensagem pode levar mais de um assunto** (D37): por exemplo, o débito
  total junto com o link de pagamento.
- **Apresenta primeiro o débito total** e só depois libera o desconto, **aos poucos** (D29).
- **Tira o cliente do Pix mês a mês** e o leva para o cartão de crédito ou débito, pelo **link de
  pagamento do CTN**, em que ele quita a dívida e já cadastra o cartão (D30).
- Passa a conversa para um humano quando o caso não é para o robô. Exemplos: o cliente quer cancelar,
  cita Procon ou advogado, diz que já pagou, contesta a dívida, está em situação vulnerável, tem
  **atrito com a clínica** ou quer pagar na **conta de energia (Energisa)**. Os detalhes estão em
  [`arquitetura.md`](arquitetura.md).

## 3. Escopo da primeira versão

- **Uma franquia: Cartão de Todos João Pessoa Sul**, em uso real (D17). Cada franquia vai ter as suas
  credenciais, separadas (D13). A expansão para outras franquias vem se o projeto der certo.
- **Serviço próprio em Python** (D7), ligado ao Chatwoot como Agent Bot (D1).
- **A carteira de inadimplentes é importada no próprio agente** (D2), dividida em **réguas** pelo número
  de mensalidades em aberto: NR, R1, R2, R3, Prévia de Desfiliado, R4, R5 e R6 (D25, D27).
- **O agente dispara a mensagem de abertura**, a que cita a dívida, pela API do disparador (D3). Ela
  leva o botão **"Pagar com Pix"**, e o Pix só é gerado quando o cliente toca nele (D26).
- **Integrado ao CTN**, o sistema com os clientes, as matrículas e o histórico. A baixa do pagamento
  entra na aba de mensalidades do CTN (D31).
- **Duas formas de pagamento:**
  - **Pix dinâmico**, gerado pela API Pix do banco do Cartão de Todos: uma cobrança por cliente, com
    txid, e confirmação por webhook (D22). Paga o **valor integral** (D34).
  - **Link de pagamento do CTN**, que quita a dívida e cadastra o cartão (D30). É a **única forma com
    desconto** (D34).
- **O atendimento humano fica no webchat**, fora do WhatsApp, com rodízio igualitário entre os
  atendentes (D32, [`ideia-webchat.md`](ideia-webchat.md)).

## 4. Regras da conversa

| Regra | Decisão |
|---|---|
| **Teto de resolução: 5 mensagens do agente.** Vale para todas as franquias. A abertura é template e não conta | D33, D14 |
| **A 6ª mensagem é fixa, sem LLM e sem Jev.** Se o cliente pagou, é a confirmação; se não pagou, manda para o canal externo com o texto da D19: *"Para o problema ser resolvido de forma mais eficaz, entre em contato conosco por esse canal de atendimento."* | D33, D19 |
| **A confirmação de pagamento sai sempre**, fora do teto, mesmo depois da 6ª. Fora da janela de 24h do WhatsApp, ela vai por template de utilidade | D33 |
| **Uma mensagem pode levar mais de um assunto.** O Jev decide a ação principal e o código anexa as que vão junto | D37 |
| **O débito total vem primeiro.** O desconto é liberado em degraus, um a cada objeção de valor, até o máximo da régua | D29, D35 |
| **Desconto máximo por régua:** nenhum na NR, R1 e R2; até R$ 90 na R3 e na R4; até R$ 75 na R5; até R$ 60 na R6. Padrão até a G1.1: Prévia com R$ 60, e mais de 6 mensalidades com a condição da R6 | D29, D35 |
| **O desconto só vale no link do CTN, com o cartão.** No Pix, o cliente paga o integral | D34 |
| **Propensão ao cartão.** Para o cliente propenso, o link do CTN vai primeiro, e a confirmação já convida a cadastrar o cartão. Para o não propenso, o Pix vai primeiro | D36 |
| **Janela de atendimento:** segunda a sexta, das 8h às 18h; sábado, das 8h às 12h. Domingo e feriado ficam fora. O sábado espera a G3 | D11, D16 |
| **Fora da janela, o agente não responde nada**, porque cada mensagem custa. O que chegou fora do horário é respondido quando a janela abre, como se tivesse chegado no horário. Os atendentes também veem essas mensagens no horário deles | D16 |
| **Toda saída do robô vai para o canal externo da franquia**, seja pelo teto ou por um gatilho do Jev. A exceção é quem desconfia de golpe: o atendente segue no próprio WhatsApp, sem link | D5, D44 |
| **O canal externo é um webchat** (widget do Chatwoot), em vez de outro WhatsApp, com rodízio igualitário entre os atendentes. Ver [`ideia-webchat.md`](ideia-webchat.md) | D32 |

**Padrões até as dúvidas fecharem** (todos são parâmetros):

| Lacuna | Padrão | Dúvida |
|---|---|---|
| Degraus do desconto | 3 degraus iguais até o máximo | G1.1 |
| Desconto na Prévia e na R6 | R$ 60 nas duas | G1.1 |
| Mais de 6 mensalidades | condição da R6 | G1.1 |
| Objeção de valor na NR, R1 e R2 | explica a condição; na 2ª objeção, humano | G1.1 |
| Pix gerado e não pago | vale 24h; sem pagamento, não há novo contato | G2.4 |
| Regra da propensão ao cartão | 3 meses seguidos de Pix, com tolerância de 3 dias | T9 |
| Mês a vencer na troca do cartão | não cobra junto | G9.2 |

## 5. Fora do escopo por enquanto

- Várias franquias ao mesmo tempo (D13, D17).
- Parcelamento: não existe (D23).
- Boleto: o desenho prevê `GENERATE_BOLETO` como não implementado, e o caso vai para o humano (G8).
- Troca para a conta de energia (Energisa): é manual e vai para o humano (D30).

## 6. Critério de aceite

Provisório, até a G4 trazer a meta de negócio:

- as conversas simuladas passam pelo Jev 3 sem reprovação final. Valem as da Lohanny quando chegarem;
  até lá, um roteiro nosso com um caso por régua;
- nenhum número chega ao cliente sem ter saído do backend;
- o teto (D33) e a janela (D11, D16) são respeitados nos testes;
- o modo sombra grava os laudos dos 3 Jevs em todo turno.

A meta de negócio (taxa de pagamento, mensagens até o pagamento, custo por cobrança) vem da G4.
