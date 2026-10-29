# Leitura às cegas — redator do agente de inadimplência

Para cada turno, marque a melhor e a pior mensagem. Os modelos têm letras fixas.

## Turno 1
**Cliente:** Ainda tá caro para mim.  
**O agente precisa:** OFFER_DISCOUNT, SEND_CTN_LINK

- **A:** Entendo, o valor com desconto é {valor_final}. Clique aqui para pagar e cadastrar o cartão: {link_ctn}
- **B:** Entendo. O valor com desconto fica em {valor_final} e é válido somente pelo link do cartão. Por lá, você paga e já cadastra o cartão: {link_ctn}
- **C:** Consigo oferecer {valor_final} com desconto, válido somente pelo link do cartão. Por ele, você paga e já cadastra o cartão: {link_ctn}
- **D:** Entendi. Para facilitar, você consegue pagar com desconto direto no link do Cartão de Todos: {link_ctn}. O valor final fica {valor_final}. Se você quiser, já dá para cadastrar o cartão por lá.
- **E:** Olá! Entendo perfeitamente. Para te ajudar a regularizar, conseguimos chegar ao valor de {valor_final} com desconto, mas essa condição é exclusiva para pagamento pelo link do cartão.

Você pode acessar por aqui para pagar e já cadastrar seu cartão: {link_ctn}
- **F:** Oi, tudo bem? Temos um valor com desconto exclusivo, que só vale ao acessar o link do cartão. Ao clicar em {link_ctn} o pagamento já cadastra seu cartão e o total será {valor_final}. Qualquer dúvida, estou à disposição.

## Turno 2
**Cliente:** Bom dia, quanto eu estou devendo?  
**O agente precisa:** INFORM_DEBT, SEND_CTN_LINK

- **A:** Olá! Seu saldo em aberto é {divida}. Para pagar e cadastrar seu cartão, acesse {link_ctn}.
- **B:** Bom dia! O valor em aberto é {divida}. Você pode pagar e cadastrar seu cartão por este link: {link_ctn}
- **C:** Bom dia! O valor em aberto é {divida}. Para pagar e já cadastrar seu cartão, acesse: {link_ctn}
- **D:** Bom dia! No momento, o valor em aberto é {divida}. Para regularizar, é só acessar o link {link_ctn} e já cadastrar o cartão para o pagamento.
- **E:** Bom dia! O valor em aberto é {divida}. Você pode realizar o pagamento e já cadastrar seu cartão por este link: {link_ctn}
- **F:** Oi, tudo bem? Seu valor em aberto é {divida}. Você pode regularizar o pagamento e cadastrar o cartão pelo link {link_ctn}. Qualquer dúvida, estou à disposição.

## Turno 3
**Cliente:** Consegue fazer um valor melhor?  
**O agente precisa:** OFFER_DISCOUNT, SEND_CTN_LINK

- **A:** Claro! Se você usar o link do cartão, o valor fica {valor_final}. Clique aqui: {link_ctn}
- **B:** Bom dia! Consigo oferecer o valor com desconto de {valor_final}, válido somente pelo link do cartão. Por lá, você paga e já cadastra seu cartão: {link_ctn}
- **C:** Consigo sim! O valor com desconto fica em {valor_final}, válido somente pelo link do cartão. Pague e já cadastre seu cartão por aqui: {link_ctn}
- **D:** Bom dia! Consigo sim: você pode pagar com desconto pelo link do Cartão de Todos. O valor final é {valor_final} e, para regularizar, é só acessar {link_ctn} e cadastrar o cartão para o pagamento.
- **E:** Olá! Consigo sim. Você pode quitar tudo por {valor_final}, mas esse valor com desconto é exclusivo para pagamento pelo link.

Acesse aqui para pagar e já cadastrar seu cartão: {link_ctn}
- **F:** Oi, tudo bem? Temos um valor com desconto especial, válido só no link do cartão. O valor final com desconto é {valor_final}. É só acessar {link_ctn} para pagar e já cadastrar o seu cartão. Qualquer dúvida, estou à disposição.

## Turno 4
**Cliente:** Me manda o link para eu pagar no cartão.  
**O agente precisa:** SEND_CTN_LINK

- **A:** Segue o link para você pagar e cadastrar seu cartão: {link_ctn}
- **B:** Claro! Segue o link para pagar no cartão e já cadastrá-lo: {link_ctn}
- **C:** Claro! Segue o link para pagar e já cadastrar seu cartão: {link_ctn}
- **D:** Olá! Segue o link para você pagar no cartão e já cadastrar: {link_ctn}. Se precisar, me avise.
- **E:** Olá! Segue o link para você realizar o pagamento e já cadastrar o seu cartão: {link_ctn}
- **F:** Claro! É só acessar este link para pagar e já cadastrar o cartão: {link_ctn}. Qualquer dúvida, estou à disposição.

## Turno 5
**Cliente:** Não consigo pagar tudo de uma vez.  
**O agente precisa:** OFFER_DISCOUNT, SEND_CTN_LINK

- **A:** Entendo que não consiga pagar tudo de uma vez. Temos uma opção com desconto que pode facilitar. Clique aqui para pagar e cadastrar seu cartão: {link_ctn}. O valor final com desconto é {valor_final}.
- **B:** Entendo. Posso oferecer o valor com desconto, válido somente pelo link do cartão. Por lá, você paga e já cadastra o cartão: {link_ctn}. O valor fica {valor_final}.
- **C:** Entendo. Para facilitar, você pode pagar {valor_final} com desconto pelo link do cartão. Nele, você realiza o pagamento e já cadastra o cartão: {link_ctn}
- **D:** Entendi! Para facilitar, você pode pagar com desconto só pelo link do cartão. O valor fica {valor_final}. Segue o {link_ctn} para você já cadastrar o cartão e concluir o pagamento.
- **E:** Olá! Para te ajudar, conseguimos um valor com desconto para você regularizar tudo: fica {valor_final}.

Você pode pagar e já cadastrar seu cartão por este link: {link_ctn}
- **F:** Oi, tudo bem? Temos um valor com desconto exclusivo para pagamento via link do Cartão de Todos. Clique no link abaixo para pagar e já cadastrar seu cartão. O valor final com desconto é {valor_final}. {link_ctn}

## Turno 6
**Cliente:** Não dá, consegue baixar mais?  
**O agente precisa:** OFFER_DISCOUNT, SEND_CTN_LINK

- **A:** Claro, o valor com desconto é {valor_final}. Clique aqui: {link_ctn}
- **B:** Entendo. Consigo oferecer o valor com desconto de {valor_final}, válido somente pelo link do cartão. Por lá, você paga e já cadastra o cartão: {link_ctn}
- **C:** Consigo oferecer {valor_final} com desconto, válido somente pelo link do cartão. Por ele, você paga e já cadastra o cartão: {link_ctn}
- **D:** Entendi. Para facilitar, você pode pagar com desconto direto no link do Cartão de Todos: {link_ctn}. O valor final é {valor_final}. Se quiser, já dá para cadastrar o cartão por lá.
- **E:** Olá! Entendo a sua situação, mas esse é o melhor valor que conseguimos liberar para você. O valor final fica em {valor_final}, mas essa condição é exclusiva para pagamento através do link do cartão.

Você pode acessar por aqui para pagar e já cadastrar seu cartão: {link_ctn}
- **F:** Oi, tudo bem? Consegui um desconto ainda maior para você. O valor final será {valor_final} e, ao acessar {link_ctn}, o pagamento já cadastra seu cartão. Qualquer dúvida, estou à disposição.

## Turno 7
**Cliente:** Oi, recebi a mensagem. Quanto está o meu débito?  
**O agente precisa:** INFORM_DEBT, GENERATE_PIX

- **A:** Olá, seu débito está em {divida}. Para pagar, copie e cole o Pix abaixo: {pix_copia_e_cola}.
- **B:** Oi! Seu valor em aberto é {divida}. Para pagar o valor integral, use o Pix copia e cola: {pix_copia_e_cola}
- **C:** Olá! Seu débito em aberto é de {divida}. Para facilitar, segue o Pix copia e cola do valor integral:

{pix_copia_e_cola}
- **D:** Olá! Seu valor em aberto é {divida}.  
Pix copia e cola: {pix_copia_e_cola}
- **E:** Olá! O valor em aberto do seu Cartão de Todos é {divida}.

Segue o Pix copia e cola para pagamento:
{pix_copia_e_cola}
- **F:** Olá! Seu débito está em {divida}. Para regularizar, basta fazer o Pix usando o código abaixo: {pix_copia_e_cola}. Qualquer dúvida, estou à disposição.

## Turno 8
**Cliente:** Prefiro pagar no Pix, pode mandar?  
**O agente precisa:** GENERATE_PIX

- **A:** Segue o Pix copia e cola para você efetuar o pagamento: {pix_copia_e_cola}
- **B:** Claro! Segue o Pix copia e cola para pagamento integral: {pix_copia_e_cola}
- **C:** Claro! Segue o Pix copia e cola para pagamento integral:

{pix_copia_e_cola}
- **D:** Claro! Segue o Pix copia e cola do valor integral: {pix_copia_e_cola}
- **E:** Com certeza! Segue o Pix copia e cola para o pagamento do valor integral:

{pix_copia_e_cola}
- **F:** Oi, tudo bem? Segue o Pix copia e cola para você efetuar o pagamento: {pix_copia_e_cola}. Qualquer dúvida, estou à disposição.

## Turno 9
**Cliente:** Qual o horário de funcionamento da clínica do centro?  
**O agente precisa:** OUT_OF_SCOPE

- **A:** Oi! Sobre o horário de funcionamento da clínica, esse assunto não é tratado aqui. Posso te direcionar para o contato correto?
- **B:** Olá! Não consigo informar o horário da clínica por aqui. Recomendo entrar em contato diretamente com a unidade do centro.
- **C:** Esse assunto não é tratado por aqui. Por favor, entre em contato diretamente com a clínica do centro para consultar o horário de funcionamento.
- **D:** Olá! Entendo sua dúvida, mas esse assunto não é tratado por aqui. Se você quiser, me diga qual é o serviço relacionado ao Cartão de Todos que precisa e eu tento te orientar.
- **E:** Olá! Por gentileza, esse assunto não é tratado por este canal. Você poderia entrar em contato diretamente com a clínica?
- **F:** Oi, tudo bem? Sobre o horário da clínica do centro, esse assunto não é tratado por aqui. Sugiro entrar em contato direto com a clínica. Obrigado!

## Turno 10
**Cliente:** Quero resolver isso hoje, como eu pago?  
**O agente precisa:** INFORM_DEBT, SEND_CTN_LINK

- **A:** Seu saldo em aberto é {divida}. Para pagar e cadastrar seu cartão, clique no link: {link_ctn}.
- **B:** Para resolver, o valor em aberto é {divida}. Você pode pagar e cadastrar seu cartão por este link: {link_ctn}
- **C:** Para resolver hoje, o valor em aberto é {divida}. Você pode pagar e já cadastrar seu cartão por este link: {link_ctn}
- **D:** Olá! Para resolver hoje, segue o valor em aberto: {divida}.  
Você pode pagar e já cadastrar o cartão por aqui: {link_ctn}.
- **E:** Olá! Para resolvermos isso agora, o valor em aberto é {divida}. Você pode realizar o pagamento e já cadastrar seu cartão por este link: {link_ctn}
- **F:** Oi, tudo bem? Seu valor em aberto é {divida}. Você pode regularizar agora mesmo acessando {link_ctn}, onde já cadastra o cartão e efetua o pagamento.

## Turno 11
**Cliente:** Tá caro, tem desconto?  
**O agente precisa:** INFORM_DEBT, GENERATE_PIX

- **A:** Olá! Seu saldo em aberto é de {divida}. Para quitar, basta usar o Pix: {pix_copia_e_cola}. Infelizmente não há desconto disponível para esta dívida. Qualquer dúvida, estamos à disposição.
- **B:** Entendo, mas não temos desconto disponível para esse valor. O total em aberto é {divida}. Para pagar o valor integral, use este Pix copia e cola: {pix_copia_e_cola}
- **C:** Entendo, mas para essa dívida não há desconto disponível. O valor em aberto é {divida}. Para facilitar, segue o Pix copia e cola do valor integral: {pix_copia_e_cola}
- **D:** Olá! Tudo bem? No momento, o valor em aberto é {divida}. Sobre desconto, infelizmente não há desconto para essa cobrança.  

Segue o Pix copia e cola do valor integral: {pix_copia_e_cola}
- **E:** Olá! Entendo seu pedido, mas para esse valor não conseguimos aplicar descontos. O total em aberto é {divida}.

Segue o Pix copia e cola para pagamento:
{pix_copia_e_cola}
- **F:** Olá, tudo bem? O valor em aberto é {divida}. Não temos desconto para essa parcela. Para facilitar, segue o Pix copia e cola: {pix_copia_e_cola}. Qualquer dúvida, estou à disposição.

## Turno 12
**Cliente:** Tá caro.  
**O agente precisa:** OFFER_DISCOUNT, SEND_CTN_LINK

- **A:** Olá! Você pode quitar a dívida com {valor_final} no link abaixo, que já inclui o desconto exclusivo para o cartão. Clique aqui: {link_ctn}
- **B:** Entendo. Consigo oferecer o valor com desconto, válido somente pelo link do cartão. Por lá, você paga e já cadastra o cartão: {link_ctn}. O valor fica {valor_final}.
- **C:** Entendo. Consigo oferecer {valor_final} com desconto, válido somente pelo link do cartão. Por ele, você paga e já cadastra o cartão: {link_ctn}
- **D:** Entendi. Para facilitar, você pode pagar com desconto direto no link do Cartão de Todos: {link_ctn}. O valor fica {valor_final}. Se quiser, já dá para cadastrar o cartão por lá.
- **E:** Olá! Para te ajudar, conseguimos um valor com desconto de {valor_final} para você regularizar agora. 

Basta acessar este link para realizar o pagamento e já cadastrar seu cartão: {link_ctn}
- **F:** Oi, entendo que esteja achando caro. Por isso, preparamos um valor com desconto exclusivo que só vale no link do cartão. Basta acessar {link_ctn} que o pagamento já cadastra seu cartão e o total será {valor_final} (com {desconto}). Qualquer dúvida, estou à disposição.

## Turno 13
**Cliente:** Tá muito caro, não tem um desconto?  
**O agente precisa:** INFORM_DEBT, GENERATE_PIX

- **A:** Olá!  
O valor em aberto é {divida}.  
Segue o Pix copia e cola do valor integral: {pix_copia_e_cola}.  
Não há desconto disponível para esta dívida.
- **B:** Entendo, mas não temos desconto disponível para essa dívida. O valor em aberto é {divida}. Para pagar o valor integral, segue o Pix copia e cola: {pix_copia_e_cola}
- **C:** Entendo, mas no momento não temos desconto disponível para essa dívida. O valor em aberto é {divida}. Para facilitar, segue o Pix copia e cola do valor integral: {pix_copia_e_cola}
- **D:** Olá! Tudo bem? No momento, o valor em aberto é {divida}. Sobre o desconto, ele não se aplica a essa cobrança, por isso vou te enviar o Pix do valor integral.  

Pix copia e cola: {pix_copia_e_cola}
- **E:** Olá! Entendo seu pedido, mas para esse valor não conseguimos aplicar descontos. O total em aberto é {divida}.

Segue o Pix copia e cola para pagamento:
{pix_copia_e_cola}
- **F:** Olá, tudo bem? O valor em aberto é {divida}. Infelizmente não há desconto disponível para essa dívida. Para facilitar o pagamento, segue o Pix copia e cola: {pix_copia_e_cola}. Qualquer dúvida, estou à disposição.
