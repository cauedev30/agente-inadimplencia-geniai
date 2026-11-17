# Políticas do agente

O que o agente faz sozinho, o que vai ao atendente e o que é defeito. É a página que uma pessoa lê; o código lê a
tabela de `src/agente/turno/catalogo.py` e os números do `politica.toml`. Vale desde a D63 ([`decisoes.md`](decisoes.md)), revista em 2026-11-17: o Luna responde a tudo o que é da cobrança
e redige a passagem ao atendente.

## 1. O princípio

**O Luna responde a tudo o que é da cobrança e redige a passagem ao atendente. O código continua conferindo tudo o
que ele devolve.** O objetivo de toda mensagem é o pagamento, no menor número de mensagens.

Por quê: até 2026-11-17 (D63) o Luna só redigia nas 4 ações de cobrança, e o resto virava frase pronta ou atendente.
O agente parecia preso a comandos: a pergunta "então esse desconto só existe se eu cadastrar meu cartão?" caía no
`NONE` e ia ao atendente, e quatro das cinco respostas do roteiro eram fixas. Dúvida, desabafo e dificuldade de pagar
são cobrança: o Luna as responde, com texto diferente a cada conversa. O atendente recebe só o que é dele, e o cliente
é avisado com uma mensagem que responde ao que ele disse. Não muda: nenhum número sai sem vir do backend, a ação tem de
ser permitida, os gatilhos de humano vencem o texto, o teto é 5 e só o piloto conversa.

O Luna escreve com marcadores (`{divida}`, `{link_ctn}`…), o backend calcula os valores e o código confere. Ações:

| Ação | O que o texto faz |
|---|---|
| `INFORM_DEBT` | apresenta o valor em aberto, sem desconto; a 1ª apresentação da dívida |
| `OFFER_DISCOUNT` | oferece o valor com desconto, que só vale no link do cartão; inclui quem diz que está sem dinheiro |
| `SEND_CTN_LINK` | manda o link em que o cliente paga e já cadastra o cartão |
| `GENERATE_PIX` | manda o Pix copia e cola do valor integral; inclui quem não quer ou não pode usar o cartão |
| `ANSWER` | responde em uma ou duas frases, com as regras da cobrança, a dúvida ou o comentário que as outras ações não cobrem (inclui o cumprimento e a pergunta sobre o valor depois de a dívida ser informada) |
| `OUT_OF_SCOPE` | diz em uma frase que este canal trata só da mensalidade em aberto, sem afirmar nada sobre o assunto perguntado |
| `NONE` | sem texto: só a confirmação ou o agradecimento, sem pergunta nem comentário (na dúvida, `ANSWER`) |

**Toda resposta leva ao pagamento**, em três camadas. No código: depois de `INFORM_DEBT`, `ANSWER` ou `OUT_OF_SCOPE`,
o cliente tem um meio de pagamento vigente na conversa (o Pix ou o link que ainda vale); se não tem, o código anexa o
Pix, ou o link para quem paga Pix todo mês, e com um meio vigente não repete o bloco. No prompt: a resposta termina no
próximo passo para pagar, apontando o meio já enviado ou oferecendo o outro. Na medição: `agente resumo-turnos` mostra
em quantas conversas o meio foi na 1ª resposta e quantas mensagens o agente usou até enviá-lo. A passagem ao atendente
não leva Pix nem link, porque quem assume é a pessoa.

## 2. A tabela de políticas

| Situação | Saída |
|---|---|
| Dúvida ou comentário sobre a cobrança ("o desconto é só no cartão?", "no Pix tem desconto?", "o link é seguro?", "você é robô?", "posso pagar dia 20?", "Oi" no meio da conversa) | o Luna responde (`ANSWER`) e leva ao pagamento |
| Dificuldade de pagar ou desabafo ("tô desempregado", "tá apertado") | o Luna acolhe em uma frase e oferece o desconto (`OFFER_DISCOUNT`); sem degrau a oferecer, responde por `ANSWER` |
| O cliente não quer ou não pode usar o cartão | o Luna responde e manda o Pix do valor integral (`GENERATE_PIX`) |
| Fora do assunto (`OUT_OF_SCOPE`): horário, endereço, clínica, outro serviço | o Luna redige e leva ao pagamento. Reprovado 2 vezes: frase fixa de reserva, sem Pix, sem link e sem atendente: *"Esse assunto não é tratado por aqui. Esta conversa é sobre a sua mensalidade em aberto."* |
| Terceiro (quem escreve não é o devedor) | frase fixa: *"Obrigado pelo retorno. Este contato é para o titular do cadastro. Quando ele puder, é só falar com a gente por aqui."* |
| Palavra-chave: golpe ou golpista; advogado, Procon ou processar | atendente, **sem chamar o Luna**, com a frase fixa (a do golpe, no golpe) |
| Sinal de saída (cancelar, risco jurídico, já pagou, contesta a dívida, desconfia de golpe, situação grave, reclama da clínica, Energisa), pedido de humano (`quer_humano`), pergunta que a cobrança não responde (`sem_resposta`: "quais meses?", vencimento, boleto), tom irritado ou hostil | atendente, com o motivo do sinal e a **passagem redigida pelo Luna**; no jurídico e no golpe, a frase fixa |
| Situação grave (doença séria, luto, acidente, crise) | atendente (`CAUTELA`), com a passagem; a falta de dinheiro comum não é este sinal |
| `NONE` sem texto, fora da lista de cortesia | atendente (`SEM_RESPOSTA`), com a frase fixa |
| `NONE` sem texto, dentro da lista de cortesia | silêncio |
| Texto de ação reprovado nas 2 tentativas | atendente (`VALIDACAO`), com a frase fixa |
| Passagem reprovada pelo conferidor | a frase fixa, sem 2ª chamada; o cliente nunca fica sem o aviso |
| Foto ou arquivo sem texto, inclusive o que chega como `[Image]` | atendente (`CONCILIACAO`), para conferir o comprovante, com a frase própria: *"Recebi o seu arquivo. Um atendente da unidade vai conferir e continuar com você aqui mesmo, nesta conversa."* |
| Só figurinha (`[Sticker]`) | silêncio por gate, sem chamar o Luna |
| A frase fixa do handoff | *"Para resolver da melhor forma, um atendente da unidade vai continuar com você aqui mesmo, nesta conversa."* É a da 6ª mensagem, do `NONE` fora da cortesia, do texto reprovado e da passagem reprovada. O golpe tem a dele (`texto_humano_no_whatsapp`) |

A passagem do Luna é uma mensagem só, gravada com origem `AGENTE` e fora do teto. Ela sai dentro da transação do
handoff, como a fixa (a pendência AUD-08, em [`depois.md`](depois.md), segue igual).

**Lista de cortesia** (fechada; comparada sem acento, sem pontuação e em minúsculas): ok, okay, blz, beleza, ta bom,
certo, entendi, combinado, obrigado, obrigada, obg, valeu, vlw, e a mensagem só de emoji. A mensagem pode juntar
termos da lista ("Ok, obrigado!", "beleza, valeu"), e a figurinha junto de uma cortesia vale como emoji; uma palavra
de fora ("muito obrigada", "bom dia") tira a mensagem da lista. Vale só se **todas** as mensagens novas do turno são
cortesia: "ok, mas quanto fica?" vai ao Luna como qualquer pergunta. "Oi" não está na lista: no meio da conversa, o
Luna cumprimenta e segue de onde parou (`ANSWER`).

Uma linha nova na tabela muda só a **saída**, dentro do mesmo tipo: trocar uma frase por outra, ou um motivo do
atendente por outro. Trocar o tipo (frase ↔ atendente) pede código (`orquestrador.py`, `_motivo_da_politica` e
`_frase_da_politica`). Uma situação nova ainda precisa de código que a detecte (um sinal, um
gate ou uma ação); no prompt fica só a linha que faz o Luna reconhecê-la. Diante de uma exceção nova, a 1ª pergunta é
"dá para mandar ao atendente?".

## 3. Regras da conversa

| Regra | Decisão |
|---|---|
| **Teto: 5 mensagens do agente.** A abertura (template) e a passagem ao atendente não contam. Depois da 5ª, a próxima mensagem do cliente recebe a **6ª, fixa e sem LLM**, e a conversa passa ao atendente | D33, D14 |
| **A 6ª** é a frase fixa do handoff, sem link e sem botão | D60, D63 |
| **A confirmação de pagamento sai sempre**, fora do teto. Fora das 24h do WhatsApp, vai por template de utilidade. Para quem paga Pix todo mês, convida a cadastrar o cartão | D33, D36 |
| **O toque em "Pagar com Pix"** é respondido na hora, mesmo fora da janela, com uma mensagem fixa e sem LLM: o Pix, o valor, o vencimento e o recebedor. Conta no teto | D26, D40, D59 |
| **Janela de atendimento:** segunda a sexta, 8h às 18h; sábado, 8h às 12h (padrão G3); domingo e feriado fora. Fora dela o agente não responde: o que chegou é respondido quando a janela abre | D11, D16 |
| **Janela de 24h do WhatsApp:** se a última mensagem do cliente passou de 24h, o agente não responde: a conversa vai ao atendente com uma nota, sem mensagem ao cliente | D16 |
| **Desconto máximo por régua:** nenhum na NR, R1 e R2 (sem ação de desconto); até R$ 90 na R3 e na R4; até R$ 75 na R5; até R$ 60 na R6. Três degraus iguais, um por objeção de valor. Esgotados os degraus, a última oferta é o valor final: o agente mantém o link, sem prometer outro desconto | D29, D35, D47 |
| **Propensão ao cartão:** quando o cliente não tem meio de pagamento vigente, o código anexa à resposta (informar o débito, `ANSWER` ou fora do assunto) o link do CTN para quem paga Pix em dia fixo há 3 meses e o Pix para os demais | D36, D37 |
| **Meio de pagamento vigente:** o último meio enviado nesta conversa que ainda vale (Pix de valor igual à dívida que não vence antes de `pix_reaproveitar_minimo_minutos`; link pendente sem vencimento ou ainda não vencido). Vigente, o código não repete o bloco; o Luna aponta para ele. Conversa nova começa sem meio | 2026-11-17 |
| **Áudio** é transcrito (`gpt-4o-mini-transcribe`) e segue como texto. Áudio que não se lê: pede o texto uma vez, fora do teto; na 2ª vez, atendente | D43 |
| **Cobrança quitada no CTN:** não se cobra; a conversa vai ao atendente conferir, com a mensagem só na 1ª vez | D50 |
| **Luna, CTN, Pix ou Chatwoot fora do ar:** 3 tentativas, depois atendente; o erro de código repetido também | D38 |

No handoff o agente deixa no Chatwoot uma nota privada, uma etiqueta com o motivo e a atribuição ao time, e abre a
conversa; ao cliente vai a passagem do Luna ou a frase fixa (seção 2). Quando o atendente marca a conversa como
Pendente, o robô reassume.

**Padrões até as dúvidas fecharem** (todos são parâmetros do `politica.toml`):

| Lacuna | Padrão | Dúvida |
|---|---|---|
| Degraus do desconto | 3 degraus iguais até o máximo | G1.1 |
| Desconto na Prévia e na R6 | R$ 60 nas duas | G1.1 |
| Mais de 6 mensalidades | condição da R6 | G1.1 |
| Pix gerado e não pago | vale 24h; sem pagamento, não há novo contato | G2.4 |
| Regra da propensão ao cartão | 3 meses seguidos de Pix, com tolerância de 3 dias | T9 |
| Mês a vencer na troca do cartão | não cobra junto | G9.2 |
| Promessa de pagamento ("pago dia 20") | o agente não registra promessa nem segura prazo ou desconto: diz que o cliente pode pagar quando puder, pedindo o Pix ou o link de novo na conversa | G11 |
| O que dizer do "corte" | nada: o Luna não cita corte, cancelamento nem consequência do atraso | G12 |

## 4. Invariantes: o que é defeito

Um invariante tem conferidor no código. Violá-lo é defeito e **bloqueia**; nada mais bloqueia, além da suíte vermelha e
do agente fora do ar.

| Invariante | Quem confere |
|---|---|
| Nenhum dígito, `R$` ou valor por extenso fora dos marcadores; nenhum link fora de `{link_ctn}` (o que se barra, logo abaixo) | `motivos_do_texto` (`turno/redacao.py`) |
| Marcador conhecido, com valor e com os obrigatórios da ação. Na `ANSWER` e no `OUT_OF_SCOPE`, `{valor_final}` e `{desconto}` só valem com degrau liberado, e `{link_ctn}` e os do Pix só valem quando o sistema anexa o meio de pagamento | `interpolar` (`turno/redacao.py`) |
| Ação dentro das permitidas no turno (`HUMAN` nunca é permitida; `OFFER_DISCOUNT` só com degrau seguinte) | `decidir` (`turno/uma_chamada.py`) |
| Sinal de saída, tom irritado ou hostil, pedido de humano e `sem_resposta` vão ao atendente | `decidir` |
| Palavra-chave (golpe, golpista, advogado, Procon, processar) vai ao atendente, com o Luna no ar ou fora. "Humano", "atendente", "falar com alguém" e "processo" **não** são palavra-chave: vão ao Luna, que marca o sinal | `PALAVRAS_CHAVE` (`turno/uma_chamada.py`) |
| **Depois de qualquer resposta do agente (`INFORM_DEBT`, `ANSWER`, `OUT_OF_SCOPE`), o cliente tem um meio de pagamento vigente na conversa**: se não tem, o código anexa o Pix ou o link; com um vigente, não repete. Única exceção: a frase fixa de reserva do fora do assunto | `anexar` (`turno/acoes.py`), com o meio lido em `meio_vigente` (`turno/executar.py`) |
| **A passagem redigida** não tem dígito, `R$`, valor por extenso, link nem marcador, e contém a palavra "atendente"; senão sai a frase fixa, sem 2ª chamada | `conferir_passagem` (`turno/uma_chamada.py`) |
| **Jurídico e golpe levam sempre a frase fixa**, mesmo que o Luna escreva uma passagem | `fazer_handoff` (`turno/handoff.py`), `MOTIVOS_DE_FRASE_FIXA` (`turno/catalogo.py`) |
| Teto de mensagens, janela de atendimento, janela de 24h, humano na conversa | `avaliar_gates` (`turno/gates.py`) |
| Só o piloto conversa | liberação (`turno/liberacao.py`) |
| O turno que chega ao Luna termina em mensagem, handoff ou silêncio da lista de cortesia | orquestrador (`_seguir`) |
| Nenhum segredo no log | a máscara do log (`src/agente/logs.py`) |

O que o `motivos_do_texto` barra fora dos marcadores: qualquer dígito, inclusive o que não é decimal ("①", "²", "½"),
e `R$`; o `%`, "por cento", "centavos" e "milhão"; o numeral por extenso de "dez" a "novecentos", "cem", "cento" e
"metade", mesmo solto ("sai por noventa", "pague a metade"); "zero", "um/uma", "dois/duas", "três" a "nove" e "mil" só
quando vêm perto de uma palavra de dinheiro ou prazo ("dois reais", "três dias") ou fazem um número composto ("mil e
quinhentos"); "http", "www" e qualquer `palavra.tld` com o tld em minúsculas (`cartaodetodos.shop`), e com maiúscula
os tlds comuns. O ponto colado entre duas frases ("Obrigado.Até logo", "agora.Com o valor") passa.

**Silêncios legítimos** (lista fechada; não violam o invariante do turno): sem cobrança ativa; humano na conversa;
fora do piloto; anexo não aceito; só figurinha; fora da janela (o turno é adiado); fora das 24h (atendente, sem
mensagem); a mensagem do agente gravada e ainda não enviada que passa das 24h na espera (o texto é descartado e a
conversa vai ao atendente, com a nota, sem mensagem ao cliente), ou que o teto fecha na espera (descartada, porque as
outras já saíram); quem já saiu pelo teto e escreveu de novo (atendente, sem mensagem); a cobrança quitada no CTN de
quem já saiu por conciliação (atendente, sem mensagem; a foto e o "já paguei" repetem a frase); a cobrança
que fecha no meio do turno; o turno em `ERRO`, que tem nova tentativa.

Regra nova só entra como invariante se ganhar conferidor no código. Senão, é estilo.

## 5. Estilo: o que é medido e nunca bloqueia

Só no prompt, sem conferência no código: usar as palavras do cliente; frases curtas, em português natural; cobrir a
ação inteira na mensagem; não repetir o valor que o cliente citou nem frase que já está no histórico; não prometer
perdão, prazo, verificação, retorno nem nada que a ação não faça; não afirmar nada sobre a rede, as clínicas e as
coberturas; dizer logo que não há parcelamento; ser respeitoso, sem ameaça nem constrangimento; "quem quer pagar,
paga"; terminar a resposta no próximo passo para pagar (o meio já enviado ou o outro); acolher em uma frase quem diz
que está sem dinheiro, antes de oferecer o desconto; distinguir a dificuldade de dinheiro da situação grave; responder
a dúvida junto do pedido na mesma mensagem; dizer que é o assistente virtual do Cartão de Todos a quem pergunta se é
robô; na passagem, acolher o que o cliente disse sem opinar se ele tem razão; os critérios de cada sinal e do tom;
"Oi" no meio da conversa (o Luna cumprimenta e segue de onde parou, `ANSWER`); ler o áudio transcrito sem comentar a
transcrição. A temperatura do Luna é 0,7, fixa no código.

## 6. As regras do prompt de hoje, uma a uma

O prompt `SISTEMA` (`turno/uma_chamada.py`, 51 linhas no fonte, depois da revisão de 2026-11-17), a
`POLITICA_COBRANCA` (9 regras), o `_FAZ_NONE` e os critérios de `sinais.py`. **Invariante** = o código confere (a linha do prompt fica, em uma frase); **estilo** = fica
no prompt, sem conferência.

| Regra | Classe |
|---|---|
| Os sinais são `true` só quando o cliente deixa claro; os critérios de cada sinal | estilo |
| O sinal de saída e o `quer_humano` mandam a conversa ao atendente | invariante (`decidir`) |
| As definições de `neutro`, `impaciente`, `irritado` e `hostil`; desabafo sem aspereza não é irritado | estilo |
| Tom irritado ou hostil vai ao atendente | invariante (`decidir`) |
| A ação é uma das permitidas; nunca `HUMAN` | invariante (`decidir`) |
| "Quem quer pagar, paga": pedido de link ou Pix, ou oferta aceita, recebe o meio de pagamento | estilo |
| O `reply` leva os marcadores obrigatórios da ação; os opcionais só se ajudarem; nenhum outro | invariante (`interpolar`) |
| Cobrir a ação inteira nesta mensagem | estilo |
| `NONE` com `reply` vazio, só para confirmação ou agradecimento; na dúvida, `ANSWER` | fica, em uma frase: o código decide entre silêncio e atendente |
| O objetivo de toda mensagem é o pagamento; a resposta termina no próximo passo para pagar, apontando o `meio_vigente` ou oferecendo o outro | estilo (o meio vigente em si é invariante: `anexar`) |
| Onde o `reply` vale: nas ações com texto e na passagem; vazio no `NONE` e no terceiro | fica, em uma frase |
| A passagem: com sinal de saída, `quer_humano`, `sem_resposta` ou tom irritado ou hostil, uma ou duas frases que acolhem o que o cliente disse e dizem que um atendente da unidade continua nesta conversa; sem marcador, valor nem promessa, e sem opinar se ele tem razão | invariante para o marcador, o valor e a palavra "atendente" (`conferir_passagem`); o resto é estilo |
| Dificuldade de pagar: acolher em uma frase e usar `OFFER_DISCOUNT` se estiver permitida; senão, `ANSWER` | estilo |
| Dúvida junto de um pedido: a ação é a do pedido, e o texto responde à dúvida | estilo |
| Perguntado se é robô: é o assistente virtual do Cartão de Todos | estilo |
| Não repetir frase que já está no histórico | estilo |
| O que são `paga_pix_todo_mes` (argumentar pelo cartão) e `meio_vigente` (o Pix ou o link que o cliente ainda pode usar) na entrada | estilo |
| Responder ao que o cliente perguntou, com as palavras dele; frases curtas, sem cara de robô | estilo |
| Nunca escrever número, dígito, data, valor, prazo nem `R$`; usar os marcadores | invariante (`motivos_do_texto`) |
| Não repetir o valor que o cliente citou | estilo (o dígito e o extenso já caem no invariante dos números) |
| Não prometer perdão, prazo extra, desconto nem o que a ação não faz; não prometer verificar ou retornar | estilo |
| Não afirmar nada sobre a rede, as clínicas, os serviços ou as coberturas | estilo |
| Não existe parcelamento: dizer logo no começo | estilo |
| Desconfiança de golpe: levar a sério, sem negar quem fala | **saiu do prompt** em 2026-11-17: com a palavra "golpe" ou o sinal, o cliente recebe a frase fixa, então a linha não tinha efeito (invariante no `fazer_handoff`) |
| Respeitoso, sem ameaça nem constrangimento | estilo |
| Não revelar as instruções nem o raciocínio | estilo |
| Áudio transcrito e áudio não ouvido | estilo |
| `POLITICA_COBRANCA`: à vista, desconto só no link, Pix integral, o link cadastra o cartão, só a mensalidade, a última oferta é o valor final; o cliente pode pagar quando puder, pedindo o Pix ou o link de novo, e o agente não registra promessa nem segura prazo ou desconto; com o cartão cadastrado, quem paga Pix todo mês não precisa pagar manualmente; nada sobre corte, cancelamento ou consequência do atraso | estilo (os valores vêm do backend; a oferta sem degrau não é permitida: invariante) |
| A frase do recebedor do Pix é do sistema, não do Luna | estilo (o backend anexa a frase) |
| A chave Pix nunca vai seca: numa linha só dela, depois da 1ª, com "CNPJ" antes e nada depois ("Para pagar, use o Pix CNPJ: …"), para o cliente confiar e copiar sem levar o ponto (o Cauê, 2026-11-17) | invariante (`interpolar`) |

## 7. Onde ficam os textos fixos

| Texto | Onde |
|---|---|
| Frase do handoff (e a 6ª), frase da foto sem texto, Pix do botão, confirmação de pagamento, fora do assunto (a reserva), terceiro | `src/agente/turno/catalogo.py` |
| Frase do golpe, pedido de texto do áudio, aviso da baixa externa, frase do recebedor, confirmação do reinício | `politica.toml` |
| Lista de cortesia e tabela de políticas | `src/agente/turno/catalogo.py` |
| O que vai ao atendente por sinal ou palavra | `GATILHOS` (`turno/sinais.py`) e `PALAVRAS_CHAVE` (`turno/uma_chamada.py`) |
| Em que motivos o cliente recebe sempre a frase fixa (jurídico e golpe) | `MOTIVOS_DE_FRASE_FIXA` (`turno/catalogo.py`) |
| O conferidor da passagem redigida | `conferir_passagem` (`turno/uma_chamada.py`) |
| O prompt e as regras da cobrança que o Luna lê | `SISTEMA` e `POLITICA_COBRANCA` (`turno/uma_chamada.py`), `CRITERIOS_DA_ACAO` e `DESCRICAO_ACOES` (`turno/sinais.py`) |
| O que barra antes do LLM | `avaliar_gates` (`turno/gates.py`) |
