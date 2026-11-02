# Runbooks de operação

Procedimentos para a stack de produção (`docker/compose.prod.yaml`, plano `12`). Cada runbook segue o formato **sintomas → diagnóstico → ação → verificação**. Na dúvida, a prioridade é a do `CLAUDE.md`: segurança/LGPD > nunca mandar mensagem indevida > nunca perder mensagem/lead > o resto. Por isso, **pausar (kill switch) é sempre seguro**; reenviar às cegas nunca é.

| Runbook | Quando usar |
| --- | --- |
| [Deploy e rollback](deploy-e-rollback.md) | Toda versão nova; versão ruim no ar |
| [Restore de backup](restore-de-backup.md) | Perda/corrupção de dados; teste mensal de restore |
| [Token da Meta inválido](token-meta-invalido.md) | Alerta `token_invalid`, erro 190, canal pausado |
| [Orçamento esgotado](orcamento-esgotado.md) | Alertas `budget:*` (50/80/100%), `BudgetDenied` |
| [Fila travada / DLQ](fila-travada-dlq.md) | Alertas `inbound_backlog`, `outbox_stuck`, `jobs_failed`, `dead_letters`, `turn_failures` |
| [Spool crescendo](spool-crescendo.md) | Alerta `spool`; `/health` com `spool_bytes` > 0 |
| [Postgres ou Redis fora](postgres-redis-fora.md) | `/health` 503, workers reiniciando |
| [Rotação de chaves](rotacao-de-chaves.md) | KEK/DEK, app secret da Meta, senhas, chave do provedor, vazamento de segredo |
| [Incidente LGPD e pedido de titular](incidente-lgpd.md) | Vazamento suspeito; titular pede acesso/eliminação |
| [Kill switch](kill-switch.md) | Bot dizendo o que não deve; qualquer incidente em curso |
| [Teste de carga](teste-de-carga.md) | Resultado e como repetir o teste de carga |
| [Observabilidade](observabilidade.md) | Ligar o perfil `obs`; painéis; o que fazer com cada alerta |
| [Go-live](go-live.md) | Da revisão independente ao primeiro cliente, com os 20 testes na conta real |

## Convenções usadas nos comandos

No servidor, a partir de `/srv/agentplatform/docker` (o diretório do compose):

```bash
C="docker compose -f compose.prod.yaml"                      # atalho
$C ps                                                        # estado e healthchecks
$C logs --since 30m worker                                   # logs (JSON do Caddy; app em texto)
$C exec postgres psql -U agentplatform -d agentplatform      # psql como dono (ignora RLS: cuidado)
$C run --rm --no-deps worker-bg agentplatform budget         # CLI da aplicação (passa pelo entrypoint de segredos)
```

- `docker compose exec` **não** passa pelo entrypoint de segredos, então o ambiente não tem `DATABASE_URL` etc. Para rodar a CLI use `run --rm --no-deps` (como acima) ou `exec <svc> sh /usr/local/bin/secrets-entrypoint.sh agentplatform ...`.
- O usuário `agentplatform` é superusuário e **ignora RLS**. Toda consulta manual deve filtrar `tenant_id` explicitamente. Nunca copie texto de mensagens (`text_enc`) para fora; `text_masked` já vem mascarado.
- Métricas: `curl -s -H "Authorization: Bearer $(cat secrets/metrics_token)" http://127.0.0.1:8000/metrics` de dentro do container `api` (o Caddy não expõe `/metrics`).
- Alertas ao operador saem a cada minuto (`ops_alerts`), por e-mail (`OPS_ALERT_EMAIL`) e/ou WhatsApp (`OPS_ALERT_WHATSAPP`); a chave do alerta (ex.: `outbox_stuck:<tenant>`) indica o runbook.
- Toda ação manual relevante deve ficar em `audit_log` (o console grava sozinho; ações por SQL: inserir uma linha com `actor_kind='operator'`).
