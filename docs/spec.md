# Spec do produto

Status: fechada. Reescrita em 2026-11-11 para o agente enxuto (D59, D60, D61). Os desenhos antigos, com o Jev, o
canal externo por link e a sombra, ficam na tag `antes-do-enxugamento`.

As decisões citadas (D1, D2, …) estão em [`decisoes.md`](decisoes.md). O que ainda falta definir está em
[`duvidas.md`](duvidas.md): enquanto uma dúvida não tem resposta, ela entra no código como **parâmetro** (com um
valor padrão) ou como **interface falsa** (o sistema de fora é simulado). O que ficou fora da linha de chegada está em
[`depois.md`](depois.md).

## 1. O problema

As franquias do Cartão de Todos usam o disparador em massa da GeniAI, pela API oficial do WhatsApp (Meta), para cobrar
clientes inadimplentes. Hoje a negociação depois do disparo é feita por atendentes humanos.

Desde 1º de outubro de 2026, a Meta cobra também as respostas dentro da janela de 24 horas, que antes eram de graça
(preço da mensagem de utilidade do país; 1.000 respostas grátis por mês por número; fontes:
[Landbot](https://landbot.io/blog/whatsapp-business-api-pricing-change-october) e
[Telnyx](https://telnyx.com/resources/whatsapp-business-api-cost), consultadas em 2026-10-15; falta confirmar no
Billing Hub de um cliente). Cada resposta custa dinheiro, e o número de mensagens até o pagamento passa a ser uma
métrica de custo.

## 2. O que o agente faz

- Conversa com o cliente inadimplente no WhatsApp, pelo Chatwoot (Agent Bot, D1), e tenta resolver o pagamento no
  menor número de mensagens. Uma mensagem pode levar mais de um assunto (D37).
- **Apresenta primeiro o débito total** e só depois libera o desconto, em degraus, um a cada objeção de valor (D29).
- **Tira o cliente do Pix mês a mês** e o leva ao cartão pelo **link de pagamento do CTN**, que quita a dívida e
  cadastra o cartão (D30). O desconto só vale no link; o Pix é sempre o valor integral (D34).
- **Passa a conversa a um atendente** quando o caso não é do robô (seção 4), na mesma conversa do WhatsApp (D60).
- Em cada mensagem do cliente, **uma chamada ao Luna** (`gpt-6-luna`, D42) lê a conversa, escolhe a ação e redige o
  texto (D59). O **código confere**: a ação é permitida, os valores vêm do backend (o texto só traz marcadores), os
  gatilhos de humano, a guarda de palavra-chave e o teto. Quem decide o que pode ser dito é o código; o Luna não
  calcula nem inventa valor. O desenho está em [`arquitetura.md`](arquitetura.md).

## 3. Escopo

- **Uma franquia: Cartão de Todos João Pessoa Sul**, em uso real (D17), com credenciais próprias (D13).
- **Só conversa quem está no piloto** (D61): a lista de telefones do `politica.toml` (`liberacao`). Quem está fora é
  ignorado, sem Luna, sem CTN e sem mensagem. A lista vazia (o padrão) não conversa com ninguém. Ir além do piloto
  será decisão nova.
- **Carteira** importada no próprio agente (D2) e dividida em réguas pelo número de mensalidades em aberto: NR, R1, R2,
  R3, Prévia, R4, R5 e R6 (D25). A dívida é a mensalidade (R$ 33,40) vezes os meses em aberto (D24); o valor citado ao
  cliente é o conferido no CTN, nunca o estimado (D49).
- **Modo teste** (D56, D61): o Luciano, a Lohanny e o Cauê conversam com 5 casos inventados (`SIM-`), sem carteira real
  e sem a chave do CTN real. Comandos: `/caso`, `/paguei`, `/nota` e o reinício. O primeiro contato abre o caso.
- **Duas formas de pagamento:** Pix dinâmico do banco do Cartão de Todos, com webhook (D22), e o link do CTN (D30).
- **Não existe:** parcelamento (D23), boleto (o caso vai ao humano), troca para a conta de energia (Energisa; é
  manual e vai ao humano, D30) e várias franquias. Sem efeito pela D60 e pela D61: o atendimento fora do WhatsApp e a
  sombra (o agente só conversa com o piloto).

**Falso até o sistema real chegar:**

| O que | Até quando | Hoje |
|---|---|---|
| Pix do banco | T1 | `PixFalso`; no teste, o texto `[TESTE] aqui viria o código Pix…` |
| Link de pagamento e de cadastro do cartão, baixa e webhook do CTN | T9 | `CTNFalso`; no teste, `[TESTE]` |
| Disparador da abertura (template) | T4 | `DisparadorFalso`; no teste, a abertura sai como resposta ao 1º contato |

A leitura do CTN (carteira, conferência, baixas) é real e está pronta, mas parada até o piloto real (D48, D61).

## 4. Regras da conversa

As regras da conversa, a tabela de políticas (o que o agente faz sozinho, o que vai ao atendente e o que fica em
silêncio), os invariantes que o código confere e a lista de estilo estão em [`politicas.md`](politicas.md), que é a
única casa delas desde a D63.

## 5. Fora do escopo por enquanto

- Várias franquias ao mesmo tempo (D13, D17).
- Parcelamento (D23), boleto (G8) e conta de energia (D30).
- Tudo o que está em [`depois.md`](depois.md): o piloto real com a carteira do CTN, o Pix e o link de verdade, o
  disparador e as correções que não bloqueiam o teste.

## 6. Critério de aceite

A linha de chegada atual, provisória até a G4 trazer a meta de negócio. Está cumprida quando:

1. o agente roda no `dev2` (o serviço, o Postgres e um túnel), em modo teste, com o Luna real, só para os telefones do
   piloto;
2. o ensaio automático (`ferramentas/demo-whatsapp/ensaiar.py --teste`) passa no `dev2` com os 5 casos do cardápio:
   reprova só o turno em `ERRO`, o turno faltando e a divergência em passo sem LLM (toque no Pix, `/paguei`, `/caso`).
   A divergência em passo com LLM e as linhas `ERROR` do log vão listadas no relatório, que o Cauê lê; a suíte
   determinística é o portão, o ensaio com o Luna real é relatório;
3. o Cauê fez o roteiro exato no celular e ele passou;
4. o Luciano e a Lohanny testaram os 5 casos em até 3 dias úteis;
5. as correções que bloqueiam, tiradas da lista da Lohanny (as `/nota`), saíram num lote só, e o resto foi para
   [`depois.md`](depois.md).

Além disso, valem sempre: nenhum número chega ao cliente sem ter saído do backend; o teto (D33), a janela (D11, D16) e
o piloto (D61) são respeitados nos testes; nenhum segredo vai ao log.

A meta de negócio (taxa de pagamento, mensagens até o pagamento, custo por cobrança) vem da G4. Cruzada a linha de
chegada, a próxima é o piloto real, que espera o Pix (T1), o link do CTN (T9) e o disparador (T4).
