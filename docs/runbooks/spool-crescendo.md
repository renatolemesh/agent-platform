# Spool de webhooks crescendo

**Como funciona (plano `07` §1):** o handler de webhook verifica a assinatura, grava em `inbound_events` e responde 200. Se o Postgres não responde, o `api` grava o corpo cru (assinado) num arquivo no volume `webhook_spool` (`/var/spool/agentplatform`) e responde 200 mesmo assim, porque a Meta desinscreve o app depois de cerca de 1 h de falhas. Quando o banco volta, o spool é reprocessado (`replay_spool`), com idempotência por `idempotency_key`: replay duplicado não duplica evento.

## Sintomas

- Alerta `spool` ("Spool de webhooks crescendo"); `GET /health` com `spool_bytes` > 0 (e `ok:false` se o banco está fora).
- Geralmente acompanha [Postgres fora](postgres-redis-fora.md).

## Diagnóstico

```bash
curl -s https://$PUBLIC_DOMAIN/health
$C exec api ls -la /var/spool/agentplatform | head
$C exec api du -sh /var/spool/agentplatform
$C ps postgres && $C logs --since 15m api | grep -i spool
df -h /var/lib/docker                                   # spool e Postgres no mesmo disco
```

## Ação

1. Restabeleça o Postgres primeiro ([postgres-redis-fora.md](postgres-redis-fora.md)). Não apague arquivos do spool: são mensagens de clientes ainda não gravadas.
2. O `api` tenta o replay sozinho a cada 30 s enquanto houver algo no spool (veja `spool replay:` nos logs). Para forçar na hora (o endpoint `POST /admin/replay-spool` exige sessão de `platform_admin` e o Caddy não expõe `/admin/*`), rode dentro do container `api`, que tem o volume do spool em escrita:
   ```bash
   $C exec api sh /usr/local/bin/secrets-entrypoint.sh python -c "import asyncio; from agentplatform.api.webhooks import Spool, replay_spool; from agentplatform.runtime import get_runtime; from agentplatform.settings import get_settings; print('replayed events:', asyncio.run(replay_spool(Spool(get_settings().spool_dir), get_runtime().adapters, None)))"
   ```
   Sem `on_persisted`, os eventos entram em `inbound_events` como `pending` e o `sweep_inbound` do próximo minuto os enfileira. Cada lote (`webhooks.<ts>.replay`) só é apagado depois de gravado; se o replay falhar ou o processo cair no meio, o lote fica no disco e é retomado na próxima tentativa (reprocessar é idempotente).
3. Disco cheio: libere espaço (`docker image prune`, logs), **nunca** o volume do spool.

## Verificação

- `spool_bytes` volta a 0; os eventos aparecem em `inbound_events` e vão para `done`.
- Conversas da janela de indisponibilidade receberam resposta ou, se velhas demais, foram para handoff.
