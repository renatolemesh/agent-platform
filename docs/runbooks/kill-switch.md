# Kill switch

**O que é:** uma linha em `kill_switches` com `is_on=true`. O despachante consulta antes de **cada** bolha e o agente antes de responder. O efeito chega em ≤ 5 s (cache do registry), sem deploy. Escopos:

| Escopo | `scope` / `scope_ref` | Efeito |
| --- | --- | --- |
| Global | `global` / nulo | Nenhum envio automático em nenhum tenant |
| Tenant | `tenant` / `<tenant_id>` | Nenhum envio automático do tenant |
| Canal | `channel` / `instagram` (com `tenant_id`) | Canal pausado (o `token_guard` liga sozinho com token inválido) |
| Feature | `feature` / `outbox` (com `tenant_id`) | Bloqueia todo envio automático do tenant no despachante (só por SQL) |
| Campanha | `campaign` / `<campaign_id>` | Pausa uma campanha |

Com o kill switch ligado, mensagens recebidas continuam sendo gravadas e o vendedor continua respondendo pelo Instagram. O que estava no outbox é cancelado ou retido pelo despachante, nunca enviado depois de velho.

## Quando ligar

- Bot dizendo algo indevido (promessa, número sem fonte, dado de outro cliente) → **tenant** (ou global, se o problema é de versão/prompt).
- Incidente LGPD, deploy ruim, comportamento estranho sem diagnóstico → **tenant/global**. Pausar é sempre seguro.
- Campanha disparando errado → **campanha**. (Kill switch por capacidade, por exemplo só comentários ou só follow-ups, ainda não existe no código: use o do tenant.)

## Como ligar

**Console** (dono do tenant): botão de kill switch na tela de conversas (escopo tenant). Fica auditado.

**SQL** (operador; para global/campanha ou se o console estiver fora):
```bash
P="$C exec postgres psql -U agentplatform -d agentplatform -c"
$P "insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (null, 'global', null, true, 'incidente <id>')
    on conflict (scope, coalesce(scope_ref, ''), coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'))
    do update set is_on = true, reason = excluded.reason, changed_at = now()"
$P "insert into audit_log (actor_kind, action, target, after) values ('operator', 'kill_switch', 'global', '{\"on\": true}')"
```
Para um tenant: `(tenant_id, 'tenant', '<tenant_id>', ...)`.

## Verificação

```bash
$P "select tenant_id, scope, scope_ref, is_on, reason, changed_at from kill_switches where is_on"
$P "select status, cancel_reason, count(*) from outbox where created_at > now() - interval '10 min' group by 1, 2"
```
Novos itens do outbox saem `cancelled` com motivo de kill switch; nenhum `sent` novo do escopo pausado.

## Como desligar

Só depois da causa corrigida (cenário no lab + `lab gate` passando, quando o problema era do agente). Console (mesmo botão) ou SQL com `is_on = false`. Revise as conversas que ficaram sem resposta durante a pausa e abra handoff para o vendedor retomar as quentes.
