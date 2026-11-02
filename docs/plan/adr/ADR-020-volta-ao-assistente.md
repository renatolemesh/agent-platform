# ADR-020 — Quando a conversa volta ao assistente

- **Status:** aceito (01/11/2026, pelo dono) · **Prioridades afetadas:** 3, 4 · **Altera:** decisão P5 do plano (`01`)

## Contexto
A revisão independente de 01/11/2026 mostrou que passar a conversa para o vendedor era uma porta de mão única. Uma queda de 15 minutos do provedor de IA tirava do assistente, para sempre, todo mundo que escreveu nesse intervalo. O vendedor recebia dois re-alertas e, depois disso, ninguém mais ficava sabendo.

## Decisão
**O motivo do handoff decide quem pode devolver a conversa.**

| Motivo | Volta sozinha? | Como |
| --- | --- | --- |
| Falha nossa: `technical_failure`, `budget_exhausted` | Sim | Depois de `handoff.auto_return_after_min` (15 min), se ninguém assumiu, até `auto_return_max_per_day` (3) vezes por conversa em 24 h. As mensagens sem resposta voltam a pendentes e são respondidas. |
| Teto da conversa: `conversation_budget` | Não | Só com "Retomar", que zera o teto daquela conversa. Os tetos do dia e total continuam valendo, e o gasto segue no ledger. |
| Pedido do cliente ou motivo de negócio | Não | Ação explícita do vendedor. |
| Vendedor assumiu (eco, botão ou console) | Nunca | Ação explícita do vendedor. |

**Cliente sem resposta.** Com a conversa parada com uma pessoa, o vendedor recebe os re-alertas de sempre. Depois de `handoff.escalate_to_owner_after_min` (4 h), o responsável pela conta é avisado, uma vez por handoff.

**"Retomar" responde o que ficou sem resposta.** Valem só as mensagens depois da última resposta real. O aviso "vou chamar o consultor" não conta como resposta (`kind = handoff_notice`).

**Avisos sem handoff (`handoffs.is_notice`).** Algumas situações exigem avisar o vendedor sem tirar a conversa de onde está: pergunta sem resposta aprovada (`knowledge_gap`), cliente que pediu para parar e voltou a escrever (`opted_out_returned`) e pedido para parar com um vendedor na conversa (`opt_out`). No máximo um aviso por motivo e conversa a cada `notice_dedupe_hours`.

**Quem pediu para parar e volta a escrever.** O assistente não responde e o vendedor é avisado. A supressão continua, e nada proativo é enviado. Se o assistente pode voltar a falar com essa pessoa é pergunta para o jurídico. Até lá, a resposta é não.

## Consequências
- (+) Queda de provedor deixa de virar perda permanente de conversa.
- (+) A promessa "vou confirmar com o consultor" passa a ser verdadeira.
- (−) Um cliente pode receber "vou pedir para fulano responder" e, 15 minutos depois, a resposta do assistente. Isso é melhor do que o silêncio.
- (−) Com a base de conhecimento vazia, o vendedor recebe um aviso por conversa ao dia. O número cai conforme a curadoria preenche a base.
- `return_to_bot_after_inactivity_min` continua existindo no schema e sem uso: voltar por inatividade do vendedor segue fora do escopo.
