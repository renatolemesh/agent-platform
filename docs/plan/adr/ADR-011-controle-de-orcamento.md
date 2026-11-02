# ADR-011 — Controle de orçamento de LLM

- **Status:** aceito

## Contexto
O orçamento começa com US$ 5 de crédito, com recarga conforme o uso e teto diário de referência de US$ 2. Nenhuma chamada de LLM pode acontecer sem checagem de orçamento (anti-padrão da §17). Chamadas paralelas não podem estourar o teto.

## Opções
1. Só a trava do OpenRouter (limite por chave ou guardrail).
2. Só a checagem no harness.
3. **As duas:** reserva/liquidação no harness + trava do OpenRouter acima do teto do harness.

## Decisão
Opção 3:
- **Reserva:** o `ModelGateway` estima o custo pelo modelo mais caro da cadeia de fallback e faz `reserve` em `budget_balances` com um `update` atômico condicionado ao teto (escopos: total, dia, conversa, turno, experimento, CI). Cada reserva tem TTL, e um sweeper libera as de workers mortos.
- **Dia em UTC**, o mesmo corte do `limit_reset: daily` do OpenRouter.
- **Teto total abaixo do crédito líquido** (US$ 4,50 contra ≈ US$ 4,74 depois da taxa de 5,5%), para degradar antes do HTTP 402.
- **Liquidação:** o `settle` usa o `usage.cost` real. Se faltar (timeout ou erro), liquida pela **estimativa** (nunca 0) e concilia depois por `GET /api/v1/generation`.
- **Avaliação e CI** passam pelo mesmo gateway, com chave e escopo próprios (ADR-008).
- **Alertas:** em 50%, 80% e 100%.
- **Degradação, em ordem:** modelo mais barato → modo só FAQ → handoff + mensagem curta.
- **Trava externa:** chave de produção com `limit` diário no OpenRouter acima do teto do harness (o schema obriga), e chave separada para o laboratório.
- **Conferência:** job horário compara o gasto registrado com `GET /api/v1/key`.

## Consequências
- (+) A degradação acontece antes do corte, e o cliente nunca fica sem resposta.
- (−) A estimativa de custo antes da chamada pode errar; o `settle` corrige, com tolerância por turno.
