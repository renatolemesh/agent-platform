# Orçamento esgotado

**Como funciona:** toda chamada de LLM/embedding/STT passa pelo `ModelGateway`, que reserva o custo estimado em `budget_balances` antes de chamar e liquida pelo custo real. Há dois níveis: os tetos da **chave** do provedor (`tenant_id` nulo, escopos `key:total` e `key:day:<data UTC>`, vindos de `LLM_TOTAL_CAP_USD`/`LLM_DAILY_CAP_USD`) e os tetos de cada **tenant** (`day:<data UTC>`, de `budget.daily_cap_usd` na config). Sem saldo, a chamada é recusada (`BudgetDenied`) e o agente degrada: resposta segura e/ou handoff, nunca silêncio.

## Sintomas

- Alertas `budget:<tenant|key>:<escopo>:0.5|0.8|1.0` ("Orçamento em 50/80/100%").
- `llm_calls` com erro de orçamento; turnos indo para handoff com resposta degradada.

## Diagnóstico

```bash
$C run --rm --no-deps worker-bg agentplatform budget          # gasto x teto de cada escopo da chave
$C exec postgres psql -U agentplatform -d agentplatform -c "
  select coalesce(t.slug, '(chave)') as quem, b.scope, b.spent_usd, b.reserved_usd, b.cap_usd
  from budget_balances b left join tenants t on t.id = b.tenant_id
  where b.scope like 'key:%' or b.scope = 'day:' || to_char(now() at time zone 'utc', 'YYYY-MM-DD') order by 1, 2"
# quem está gastando: custo por conversa nas últimas 24 h
$C exec postgres psql -U agentplatform -d agentplatform -c "
  select l.tenant_id, t.conversation_id, count(*) as calls, sum(l.cost_usd) as usd
  from llm_calls l left join turns t on t.id = l.turn_id
  where l.created_at > now() - interval '24 hours' group by 1, 2 order by usd desc nulls last limit 15"
```

No console: **Orçamento** mostra gasto por escopo e por dia.

## Ação

1. Confirme que a degradação está funcionando: clientes recebem resposta segura ou vão para o vendedor (`handoffs` recentes), ninguém fica sem resposta.
2. Investigue a causa antes de aumentar: pico legítimo (campanha, anúncio), loop (mesma conversa com dezenas de turnos), abuso (um contato mandando centenas de mensagens). Loop ou abuso: kill switch da feature/tenant ou supressão do contato.
3. Pico legítimo:
   - **Teto do tenant:** console → **Orçamento** → recarga. A recarga vira uma proposta de config que outra pessoa aprova (auditado).
   - **Teto da chave:** recarregue créditos no painel do OpenRouter (e ajuste o limite lá); depois aumente `LLM_TOTAL_CAP_USD`/`LLM_DAILY_CAP_USD` em `docker/prod.env` e `$C up -d api worker worker-bg`. A trava da chave fica sempre **acima** da soma dos tetos dos tenants.
4. Reservas presas (worker morto no meio de uma chamada) são liberadas pelo `budget_sweeper` a cada minuto; não zere `reserved_usd` à mão.

## Verificação

- Novos turnos com `llm_calls` sem erro de orçamento; o alerta não se repete (alertas de orçamento são enviados uma vez por limiar).
- `agentplatform budget` mostra folga coerente com o gasto diário esperado.
