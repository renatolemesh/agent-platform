# 12 — Docker e operação

## 1. Papéis e containers

Os papéis são separados no código e nas filas, mas no MVP rodam em poucos containers. Escalar = `docker compose up --scale worker=N`, sem mudar código, porque nenhum serviço guarda estado em memória.

| Serviço | Imagem | Papel | Réplicas MVP | `mem_limit` (VPS 4 GB) |
| --- | --- | --- | --- | --- |
| `caddy` | `caddy:2-alpine` (digest fixo) | TLS automático, proxy para `api` e console | 1 | 64 MB |
| `api` | `app:<sha>` (`python:3.12-slim`, usuário não root) | Webhooks (fast-ack), Admin API, console (Jinja/HTMX) | 1 (2 em staging) | 320 MB |
| `worker` | `app:<sha>` | Filas `turns`, `comments`, `media`, `dispatch`, `echo` (procrastinate) | 1 | 640 MB |
| `worker-bg` | `app:<sha>` | Filas `followups`, `ingest`, `lab`, `maintenance` + tarefas periódicas (scheduler, sweeper de reservas de orçamento, `token_guard`, reinscrição de webhooks, retenção) | 1 | 512 MB |
| `postgres` | imagem própria `FROM pgvector/pgvector:0.8.7-pg17-bookworm` (tag a confirmar no Docker Hub) + `wal-g` | Fonte da verdade, fila, checkpoints, vetores | 1 | 1.024 MB (`shared_buffers=256MB`, `work_mem=8MB`, `max_connections=60`) |
| `redis` | `redis:7-alpine` | Debounce, rate limit, cache, pub/sub | 1 | 128 MB (`maxmemory 96mb`, `noeviction`; cache sempre com TTL; chaves reconstruíveis) |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | Coleta de traces e métricas | 1 | 128 MB |
| `prometheus` + `loki` + `grafana` | oficiais | Métricas, logs e dashboards (perfil `obs`) | 1 | 480 MB no total |
| `ocr` (perfil `tools`) | `jbarlow83/ocrmypdf` + `tesseract-ocr-por` | OCR sob demanda, fora do horário | 0 (sob demanda) | 1.024 MB |

Total residente no perfil `prod` (sem `tools`): ≈ 3,3 GB → cabe na VPS de 4 GB com 2 GB de swap; Docling e OCR pesados rodam sob demanda (perfil `tools`) ou na máquina do operador.

## 2. Compose (rascunho)

```yaml
name: agentplatform
x-app: &app
  image: ghcr.io/<org>/agentplatform:${APP_SHA}
  env_file: [.env.runtime]          # non-secret settings only
  secrets: [db_password, redis_password, openrouter_key, meta_app_secret, kek_age_key, whatsapp_token, smtp_password]
  read_only: true
  tmpfs: [/tmp]
  restart: unless-stopped
  depends_on:
    postgres: { condition: service_healthy }
    redis: { condition: service_healthy }
  networks: [internal, egress]      # egress: outbound HTTPS to Meta, OpenRouter, WhatsApp (review #20)

services:
  caddy:
    image: caddy:2-alpine
    ports: ["443:443", "80:80"]
    volumes: [./Caddyfile:/etc/caddy/Caddyfile:ro, caddy_data:/data]
    networks: [internal, edge]
    profiles: [prod, staging]
  api:
    <<: *app
    command: ["uvicorn", "agentplatform.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
    healthcheck: { test: ["CMD", "python", "-m", "agentplatform.health", "api"], interval: 15s, retries: 3 }
    mem_limit: 320m
    volumes: [webhook_spool:/var/spool/agentplatform]   # durable spool when Postgres is down (07 §1)
    ports: ["127.0.0.1:${API_PORT:-8100}:8000"]      # dev only; prod goes through caddy
  worker:
    <<: *app
    command: ["procrastinate", "--app=agentplatform.jobs.app", "worker", "--queues=turns,comments,media,dispatch,echo", "--concurrency=8"]
    healthcheck: { test: ["CMD", "python", "-m", "agentplatform.health", "worker"], interval: 30s }
    mem_limit: 640m
  worker-bg:
    <<: *app
    command: ["procrastinate", "--app=agentplatform.jobs.app", "worker", "--queues=followups,ingest,lab,maintenance", "--concurrency=2"]
    mem_limit: 512m
  postgres:
    build: ./postgres            # FROM pgvector/pgvector:0.8.7-pg17-bookworm + wal-g binary (pinned, checksum verified)
    image: agentplatform-postgres:pg17-pgvector0.8.7-walg
    environment: { POSTGRES_PASSWORD_FILE: /run/secrets/db_password, POSTGRES_DB: agentplatform }
    secrets: [db_password]
    command: ["postgres", "-c", "shared_buffers=256MB", "-c", "max_connections=60", "-c", "wal_level=replica", "-c", "archive_mode=on", "-c", "archive_command=/usr/local/bin/wal-g wal-push %p"]
    volumes: [pg_data:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 10s, retries: 5 }
    mem_limit: 1024m
    ports: ["127.0.0.1:${PG_PORT:-55432}:5432"]       # dev only
    networks: [internal]
  redis:
    image: redis:7-alpine
    command: ["sh", "-c", "redis-server --requirepass \"$$(cat /run/secrets/redis_password)\" --maxmemory 96mb --maxmemory-policy noeviction --appendonly no"]
    # noeviction: rate-limit buckets must never be evicted (review #20); cache keys all have TTL; alert at 80% memory
    secrets: [redis_password]
    healthcheck: { test: ["CMD-SHELL", "redis-cli -a \"$$(cat /run/secrets/redis_password)\" ping"], interval: 10s }
    mem_limit: 128m
    ports: ["127.0.0.1:${REDIS_PORT:-56379}:6379"]    # dev only
    networks: [internal]
  # otel-collector, prometheus, loki, grafana: profile "obs"; ocr: profile "tools"

networks: { internal: { internal: true }, edge: {}, egress: {} }
volumes: { pg_data: {}, caddy_data: {}, webhook_spool: {} }   # webhook_spool mounted rw only in api (07 §1)
secrets:
  db_password: { file: ./secrets/db_password }
  # ... one file per secret, mode 0400, decrypted from sops at deploy time
```

- **Perfis:** `dev` (portas locais 55432/56379/8100), `staging` e `prod` (sem portas publicadas além do Caddy), `obs`, `tools`.
- **Migrações:** Alembic, rodadas por um job `migrate` (`docker compose run --rm api alembic upgrade head`) antes do deploy; migrações sempre compatíveis com a versão anterior (expand/contract).
- **Deploy:** imagem construída no CI com digest; `docker compose pull && migrate && up -d`; healthcheck verde; rollback = `APP_SHA` anterior.

## 3. Backup e restore

- `wal-g` com archiving contínuo + base backup diário para object storage (S3 compatível, cifrado), retenção de 14 dias e PITR.
- `pg_dump -Fc` semanal adicional (portabilidade).
- **Teste de restore mensal automatizado:** um job sobe um Postgres temporário, restaura o último backup, roda `select count(*)` nas tabelas críticas + checagem de checksum, e registra o resultado; alerta se falhar.
- Redis não tem backup (nada nele é fonte da verdade).

## 4. Runbooks de incidente

| Incidente | Detecção | Ação imediata | Recuperação |
| --- | --- | --- | --- |
| **Token da Meta inválido/vencido** (erro 190) | Alerta crítico + `channel_accounts.token_status=invalid`; kill switch automático do canal do tenant | Avisar o dono do tenant (WhatsApp/e-mail) com o link da tela de reconexão | Dono refaz o Facebook Login → token novo cifrado → `debug_token` ok → kill switch OFF → outbox retido revalidado (o que passou da janela ou ficou velho é cancelado) → conferir webhooks inscritos (`GET /{page}/subscribed_apps`) |
| **Orçamento esgotado** | Alertas 50/80/100%; `budget_denied` em `llm_calls` | Confirmar que a degradação está ativa (FAQ-only/handoff); nenhum cliente sem resposta | Recarregar créditos no OpenRouter; ajustar `budget.daily_cap_usd` (auditado, abaixo da trava da chave); investigar a causa (pico legítimo, loop, abuso) no painel de custo por conversa |
| **App desinscrito dos webhooks** / nenhum webhook em 30 min | `webhook_last_received_timestamp` | Checar `GET /{page}/subscribed_apps` e o status do app no dashboard da Meta | Reinscrever; reconciliar com `GET /{page}/conversations` (últimas 20 mensagens por conversa ativa) e criar `inbound_events` faltantes |
| **OpenRouter fora** | Circuit breaker aberto; `llm_errors_total` | Turnos caem em handoff com mensagem segura | Reabre sozinho; se > 30 min, ligar o modo "FAQ-only" pelo kill switch `agent.respond` |
| **Fila travada / DLQ** | `queue_lag_seconds`, DLQ > 0 | Ver jobs `failed` no console | Corrigir a causa, reprocessar pelo console |
| **Worker preso** | Heartbeat do worker vencido | procrastinate reenfileira o job stalled (lease novo); se repetir, `docker compose restart worker` | Investigar o trace do turno |
| **Disco > 80%** | Alerta | Rodar retenção, limpar imagens antigas (`docker image prune`) | Ajustar retenção ou aumentar o disco |
| **Vazamento suspeito / bot falando o que não deve** | Revisão, alerta de guard, reclamação | **Kill switch** do tenant ou global (efeito em ≤ 5 s); posse PAUSED | Analisar o trace, criar cenário, corrigir, passar no gate, religar |
| **Restore** | — | Parar só os workers; o `api` continua recebendo webhooks no spool local (a desinscrição acontece após 1 h de falhas, então o `api` não pode ficar fora) | `wal-g backup-fetch` + PITR → subir → replay do spool → backfill de conversas (`/conversations`) e comentários (`/{media-id}/comments`) → conferir `subscribed_apps` |

## 5. Servidor

Premissa do plano: VPS dedicada de 2 vCPU / 4 GB / 40 GB SSD, Ubuntu 24.04, Docker Engine + Compose v2, usuário de deploy sem root, UFW (só 22 de IPs do operador, 80 e 443), `unattended-upgrades`, fuso do host irrelevante (a aplicação usa `America/Sao_Paulo` explicitamente).

Produção pede uma VPS dedicada: não coabite com outros projetos, e confira carga, memória e disco antes de usar.
