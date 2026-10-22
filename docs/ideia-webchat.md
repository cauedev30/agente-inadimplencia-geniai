# Webchat como canal de atendimento humano

> **Status: aprovado em 2026-10-21 → D32.** A operação aceitou que o atendimento humano saia do
> WhatsApp para um canal externo, com rodízio igualitário entre os atendentes. Das perguntas do fim
> deste documento, a 1 está respondida; as outras três continuam na G7 de [`duvidas.md`](duvidas.md).

## A ideia

Quando o agente não resolve em 5 mensagens (D33), a 6ª mensagem fixa (D19) manda o cliente para um
**atendente humano**. A pergunta é por onde esse atendimento acontece.

- **Outro número de WhatsApp, fora da API oficial:** descartado na avaliação do Cauê. O contexto não
  fica salvo e a auditoria fica muito ruim.
- **Webchat:** o cliente clica num link e continua a conversa numa página web. O contexto vai junto, e
  é como se tudo continuasse no mesmo ticket.

**Desenho proposto:** o cliente fala pelo **webchat**, que é o widget do canal "Website" do Chatwoot. O
atendente responde **pelo Chatwoot**, como já faz hoje. Colocar os dois no webchat não compensa,
porque seria construir uma tela de atendente que o Chatwoot já tem.

## Pontos positivos

1. **Custo.** Desde 01/10/2026 cada resposta no WhatsApp custa. A negociação com o humano, que vem
   depois da 6ª mensagem, tende a ser a parte mais longa da conversa. No webchat essas mensagens
   **não passam pela Meta e não custam**.
2. **Auditoria.** Um segundo número fora da API oficial deixa a conversa num aparelho, fora do sistema,
   e corre risco de banimento. O webchat fica **dentro do Chatwoot**, na mesma conta, com o mesmo
   histórico e os mesmos relatórios.
3. **Contexto.** O atendente abre a conversa e já vê o histórico com o robô e a nota de handoff com o
   laudo do Jev, que explica por que o robô saiu. O cliente não precisa repetir nada.
4. **Já existe no Chatwoot.** O canal "Website" é exatamente o desenho de cliente no webchat e atendente
   no Chatwoot.

## Pontos negativos e como resolver

### 1. Atrito: o cliente pode não clicar

Ele sai do WhatsApp e vai para o navegador. Um inadimplente que já resistiu a 5 mensagens pode
simplesmente não clicar.

- **Botão no lugar de link solto.** A 6ª mensagem vai como mensagem interativa com botão (*CTA URL*),
  por exemplo "Falar com um atendente". O texto continua fixo (D19). Inadimplente costuma ter medo de
  golpe, e um botão passa mais confiança que uma URL crua.
- **Domínio conhecido.** O link abre num domínio da franquia ou da GeniAI, nunca num encurtador
  genérico.
- **Ao abrir, a conversa continua.** O cliente vê um resumo do que já foi falado (a dívida e a última
  proposta) e um "olá" do atendente. Tem que parecer continuação, não recomeço.
- **Medir e ter plano B.** Sem clique em X horas, a conversa no WhatsApp ganha a etiqueta "redirecionado
  sem clique" e fica visível para o atendente decidir. A taxa de clique entra no critério de sucesso
  (G4).

### 2. A resposta do atendente pode não chegar

O webchat não tem notificação como o WhatsApp. Se o cliente fecha a aba e o atendente responde depois,
ele pode nunca ver. É o ponto mais fraco da ideia.

- **Atender em tempo real.** A 6ª mensagem só sai dentro da janela (D16), então basta ter um
  **atendente de plantão** nesse horário. O canal de webchat do Chatwoot mostra o horário de
  atendimento e o tempo de resposta esperado.
- **O link serve para voltar à conversa.** Com a validação de identidade do Chatwoot, o mesmo link
  reabre a mesma conversa, mesmo que o cliente tenha fechado a aba. Por isso o link **não é de uso
  único**: ele vale para aquela pessoa enquanto a cobrança estiver aberta.
- **E-mail como reforço.** O widget pode pedir um e-mail, e o Chatwoot manda a resposta do atendente
  por e-mail quando o cliente não está na página.
- **Último recurso: um template no WhatsApp.** Se o atendente respondeu e o cliente não voltou em X
  horas (o Chatwoot registra quando ele viu a conversa pela última vez), sai um template do tipo "Você
  tem uma resposta do atendente", com o link. Custa uma mensagem, mas só quando for preciso.

### 3. "Mesmo ticket" não vem pronto

No Chatwoot, cada canal tem as suas conversas. O webchat abre uma conversa nova do mesmo contato, e a
anterior fica visível no histórico dele. Para virar de fato um ticket só, o **ID da cobrança** do nosso
backend faz o papel de "session ID":

1. O backend cria o ID da cobrança e grava na conversa do WhatsApp, no contato e dentro do link.
2. Quando a conversa do webchat abre, o backend:
   - põe o mesmo ID nela;
   - deixa uma nota privada com o resumo (dívida, propostas feitas, laudo do Jev e link para a conversa
     do WhatsApp);
   - aplica a etiqueta e atribui ao mesmo time;
   - marca a conversa do WhatsApp como transferida, com o link para a do webchat.
3. Se o atendente gerar um Pix no webchat, ele sai pelo mesmo backend, com o mesmo ID. O webhook do
   banco (D22) fecha tudo de uma vez.

Para o atendente, fica um ticket só.

### 4. Segurança do link

O link identifica o cliente sem login. Se outra pessoa abrir, ela vê a dívida de alguém, o que bate de
frente com o CDC (art. 42) e com a LGPD.

1. **Token assinado e com validade.** O link carrega só um token, assinado pelo backend, com o ID da
   cobrança e a data de expiração. Nenhum dado aparece em claro na URL.
2. **Validação no servidor.** A página confere o token e só então carrega o widget, já identificado. A
   chave da validação de identidade do Chatwoot (HMAC) fica no servidor e nunca vai para o navegador.
3. **Confirmação antes de mostrar a dívida.** Por exemplo, os 3 primeiros dígitos do CPF, com limite de
   tentativas. Isso barra quem recebeu o link encaminhado.
4. **Nada sensível exposto.** Sem valor da dívida no título da página, na URL ou no cache.
5. **Fim da cobrança, fim do link.** Quando a cobrança encerra (pagou ou foi fechada), o token é
   revogado.

## Prioridades

| Prioridade | Itens |
|---|---|
| **Obrigatório na v1** | Segurança do link (4) e amarração do ticket (3), porque sem eles a ideia não para de pé. Da parte 2, o link que reabre a conversa e o plantão na janela |
| **Depois, se os números pedirem** | O e-mail e o template de último recurso (2), e o plano B de quem não clica (1) |

O que precisa ser construído é pequeno: uma página que valida o token e carrega o widget, e o
amarrador de conversas no backend. O resto já existe no Chatwoot ou na API oficial do WhatsApp.

## Para levar ao gestor

1. A franquia aceita que o atendimento humano saia do WhatsApp e vá para um **webchat**?
2. Dá para ter **um atendente de plantão** na janela (segunda a sexta, das 8h às 18h; sábado, das 8h
   às 12h) para responder o webchat em tempo real?
3. A **confirmação pelos 3 primeiros dígitos do CPF** antes de mostrar a dívida é aceitável para os
   clientes?
4. Em qual **domínio** a página do webchat fica: um da franquia, do Cartão de Todos ou da GeniAI?
