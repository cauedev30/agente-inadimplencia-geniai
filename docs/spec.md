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

| Regra | Decisão |
|---|---|
| **Teto: 5 mensagens do agente.** A abertura (template) não conta. Depois da 5ª, a próxima mensagem do cliente recebe a **6ª, fixa e sem LLM**, e a conversa passa ao atendente | D33, D14 |
| **A 6ª diz:** *"Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui mesmo, nesta conversa."* Sem link e sem botão. Se o Luna redigiu um texto de encaminhamento, ele vem antes dessa frase | D60, D19 |
| **A confirmação de pagamento sai sempre**, fora do teto. Fora das 24h do WhatsApp, vai por template de utilidade. Para quem paga Pix todo mês, convida a cadastrar o cartão | D33, D36 |
| **O toque em "Pagar com Pix"** é respondido na hora, mesmo fora da janela, com uma mensagem fixa e sem LLM: o Pix, o valor, o vencimento e o recebedor. Conta no teto | D26, D40, D59 |
| **Janela de atendimento:** segunda a sexta, 8h às 18h; sábado, 8h às 12h (padrão G3); domingo e feriado fora. Fora dela o agente não responde: o que chegou é respondido quando a janela abre | D11, D16 |
| **Janela de 24h do WhatsApp:** se a última mensagem do cliente passou de 24h (por exemplo, chegou no sábado depois das 12h), o agente não responde: a conversa vai ao atendente com uma nota, sem mensagem ao cliente | D16 |
| **Desconto máximo por régua:** nenhum na NR, R1 e R2 (sem ação de desconto); até R$ 90 na R3 e na R4; até R$ 75 na R5; até R$ 60 na R6. Três degraus iguais, um por objeção de valor. Esgotados os degraus, a última oferta é o valor final: o agente mantém o link, sem prometer outro desconto | D29, D35, D47 |
| **Propensão ao cartão:** ao informar o débito, o código anexa o link do CTN para quem paga Pix em dia fixo há 3 meses e o Pix para os demais | D36, D37 |
| **Áudio** é transcrito (`gpt-4o-mini-transcribe`) e segue como texto. Áudio que não se lê: pede o texto uma vez, fora do teto; na 2ª vez, atendente. Foto ou PDF sem texto vai ao atendente conferir o comprovante | D43 |
| **Cobrança quitada no CTN:** não se cobra; a conversa vai ao atendente conferir, com a mensagem só na 1ª vez | D50 |

**Quando a conversa vai ao atendente.** O Luna devolve sinais junto com a ação, e o código também confere palavras-chave
(golpe, advogado, Procon, processo, "atendente", "humano"): quer cancelar, cita risco jurídico, diz que já pagou
(CONCILIAÇÃO), contesta a dívida, desconfia de golpe, está em situação vulnerável, reclama da clínica, quer pagar na
Energisa, pede uma pessoa ou escreve em tom irritado ou hostil. Também vão ao atendente: o teto, o texto reprovado
duas vezes (depois de uma resposta segura, que sai uma vez por conversa), o Luna, o CTN, o Pix ou o Chatwoot fora do ar
depois de 3 tentativas, e o erro de código repetido. Quem escreve em nome de terceiro não vai ao atendente: recebe só
uma mensagem curta, sem falar da dívida.
No handoff o agente deixa no Chatwoot uma nota privada, uma etiqueta com o motivo e a atribuição ao time, e abre a
conversa. Quem desconfia de golpe recebe uma frase própria, sem a fixa do teto (D44). Quando o atendente marca a
conversa como Pendente, o robô reassume.

**Padrões até as dúvidas fecharem** (todos são parâmetros do `politica.toml`):

| Lacuna | Padrão | Dúvida |
|---|---|---|
| Degraus do desconto | 3 degraus iguais até o máximo | G1.1 |
| Desconto na Prévia e na R6 | R$ 60 nas duas | G1.1 |
| Mais de 6 mensalidades | condição da R6 | G1.1 |
| Pix gerado e não pago | vale 24h; sem pagamento, não há novo contato | G2.4 |
| Regra da propensão ao cartão | 3 meses seguidos de Pix, com tolerância de 3 dias | T9 |
| Mês a vencer na troca do cartão | não cobra junto | G9.2 |

## 5. Fora do escopo por enquanto

- Várias franquias ao mesmo tempo (D13, D17).
- Parcelamento (D23), boleto (G8) e conta de energia (D30).
- Tudo o que está em [`depois.md`](depois.md): o piloto real com a carteira do CTN, o Pix e o link de verdade, o
  disparador e as correções que não bloqueiam o teste.

## 6. Critério de aceite

A linha de chegada atual, provisória até a G4 trazer a meta de negócio. Está cumprida quando:

1. o agente roda no `dev2` (o serviço, o Postgres e um túnel), em modo teste, com o Luna real, só para os telefones do
   piloto;
2. o ensaio automático (`ferramentas/demo-whatsapp/ensaiar.py --teste`) passa no `dev2` com os 5 casos do cardápio,
   sem divergência, erro ou turno faltando;
3. o Cauê fez o roteiro exato no celular e ele passou;
4. o Luciano e a Lohanny testaram os 5 casos em até 3 dias úteis;
5. as correções que bloqueiam, tiradas da lista da Lohanny (as `/nota`), saíram num lote só, e o resto foi para
   [`depois.md`](depois.md).

Além disso, valem sempre: nenhum número chega ao cliente sem ter saído do backend; o teto (D33), a janela (D11, D16) e
o piloto (D61) são respeitados nos testes; nenhum segredo vai ao log.

A meta de negócio (taxa de pagamento, mensagens até o pagamento, custo por cobrança) vem da G4. Cruzada a linha de
chegada, a próxima é o piloto real, que espera o Pix (T1), o link do CTN (T9) e o disparador (T4).
