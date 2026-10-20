# Decisões

Cada decisão tem um número fixo. Uma decisão revisada não é apagada: ela ganha a marca de qual decisão
a revisou.

| # | Data | Decisão |
|---|---|---|
| D1 | 2026-10-15 | Serviço próprio no servidor de produção, ligado ao Chatwoot como **Agent Bot** |
| D2 | 2026-10-15 | A **carteira de inadimplentes é importada no próprio agente** |
| D3 | 2026-10-15 | O agente **dispara a mensagem de abertura** (a que cita a dívida) **pela API do disparador** |
| D4 | 2026-10-15 | **Pix dinâmico** de um PSP com **webhook de confirmação**: um PSP só, recebendo na conta de cada franquia |
| D5 | 2026-10-15 | Toda saída do robô, seja pelo teto ou por gatilho do Jev, manda o cliente para o **canal externo da franquia** |
| D6 | 2026-10-15 | O LLM de redação é **escolhido por teste**. Candidato: Groq `gpt-oss-120b` |
| D7 | 2026-10-19 | **Stack em Python**, mesmo com outros projetos da GeniAI em Go. O Go entra só se surgir gargalo, porque tende a render mais com muitos clientes ao mesmo tempo. Ver a análise abaixo |
| D8 | 2026-10-19 | **Teto de 3 mensagens de resolução** do agente. Na 4ª, o cliente é encaminhado para o canal externo |
| D9 | 2026-10-19 | Repositório: este, `cauedev30/agente-inadimplencia-geniai` |
| D10 | 2026-10-19 | *(Revisada pela D21.)* Repositório público por enquanto; só o Cauê trabalha nele |
| D11 | 2026-10-19 | *(Revisada pela D16 na parte de fora da janela.)* **Janela de atendimento:** segunda a sexta, das 8h às 18h; sábado, das 8h às 12h. Fora dela, uma mensagem reforçaria o horário |
| D12 | 2026-10-19 | **LGPD:** o uso do Jev com os dados dos clientes está autorizado. Zero Data Retention e No Training ligados em cada chamada |
| D13 | 2026-10-19 | *(Corrigida pela D17.)* Uma franquia só, em teste. Cada franquia terá as suas credenciais, separadas; a divisão em várias franquias fica para quando o produto escalar |
| D14 | 2026-10-19 | O teto de 3 mensagens vale para **todas as franquias**, sem configuração por franquia |
| D15 | 2026-10-19 | O repositório fica público por enquanto e vai virar **privado em breve** |
| D16 | 2026-10-19 | **Fora da janela, o agente não responde nada**, porque cada mensagem custa. O que chega fora do horário é respondido quando a janela abre, como se tivesse chegado no horário. Os atendentes também veem essas mensagens no horário deles. **Domingo e feriado ficam fora da janela** |
| D17 | 2026-10-19 | **A primeira franquia é o Cartão de Todos João Pessoa Sul, em uso real**, não teste. Se o projeto der certo, expande para outras |
| D18 | 2026-10-19 | O canal externo é um **web chat ou outro número de WhatsApp** da franquia |
| D19 | 2026-10-19 | **A 4ª mensagem é fixa, sem LLM e sem Jev:** *"Para o problema ser resolvido de forma mais eficaz, entre em contato conosco por esse canal de atendimento."*, com o link do canal externo |
| D20 | 2026-10-19 | Sem restrição de dado no repositório enquanto ele for público, porque ele vira privado em breve |
| D21 | 2026-10-19 | O projeto é feito pelo Cauê **junto com outro dev** |

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
