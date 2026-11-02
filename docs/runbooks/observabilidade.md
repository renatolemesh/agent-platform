# Observabilidade (perfil `obs`)

**Peças (plano `10` §1 e `12` §1):** `prometheus` lê `GET /metrics` do `api` a cada minuto (o `api` calcula os números no banco na hora da coleta) e avalia as regras de `docker/obs/prometheus/rules/`; `grafana` mostra os dois painéis de `docker/obs/grafana/dashboards/`; `otel-collector` recebe os spans do app por OTLP/HTTP e os transforma em contadores e latência para o Prometheus. Nenhum span é guardado: só nome, tipo, status e serviço viram label. Nas métricas há UUID de tenant, nome de fila e status, nunca texto de cliente.

| Serviço | Porta no host | Memória (limite) | Medido em repouso |
| --- | --- | --- | --- |
| `grafana` | `127.0.0.1:8130` | 288 MB | ≈ 275 MB, encostado no limite sem ser morto: o runtime cede cache antes |
| `prometheus` | `127.0.0.1:8190` | 192 MB | ≈ 60 MB |
| `otel-collector` | `127.0.0.1:8143` só em dev; em produção, sem porta publicada | 128 MB | ≈ 45 MB |

**Dois limites que você precisa conhecer:**

- **Os alertas do Prometheus não avisam ninguém sozinhos.** Não há Alertmanager neste perfil: eles aparecem em `http://127.0.0.1:8190/alerts` e no painel "Alertas disparando agora". O que chega por e-mail e WhatsApp continua sendo o alerta do próprio app, e queda total continua sendo coberta pelo vigia externo (`HEARTBEAT_URL`).
- **Não há logs aqui** (o Loki do plano ficou de fora): use `$C logs`. Também não há métrica de disco: **nada vigia hoje o disco > 80% do plano `12` §4**. Confira com `df -h /var/lib/docker`.

## Ligar em produção

A partir de `/srv/agentplatform/docker`, com `C="docker compose -f compose.prod.yaml"` e as variáveis do deploy no ambiente:

```bash
set -a; . ./.deploy.env; set +a; export APP_IMAGE="$(tail -n 1 .deployed)"
# primeira vez: a senha do admin do Grafana (o metrics_token já existe, vem do init-secrets.sh)
( umask 077; openssl rand -hex 32 | tr -d '\n' > secrets/grafana_admin_password ); chmod 0444 secrets/grafana_admin_password
$C --profile obs up -d
$C --profile obs ps
```

Acesso só por túnel SSH, nunca abra essas portas no UFW:

```bash
ssh -L 8130:127.0.0.1:8130 -L 8190:127.0.0.1:8190 deploy@<servidor>
# http://127.0.0.1:8130 (Grafana: usuário admin, senha em secrets/grafana_admin_password)
# http://127.0.0.1:8190 (Prometheus, sem senha: por isso só no loopback)
```

Para ter os números de tracing, defina `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` em `prod.env` e recrie o app (`$C up -d api worker worker-bg`), num momento calmo. Sem isso, os painéis de tracing ficam vazios e todo o resto funciona.

O `deploy.sh` não sobe nem derruba este perfil: `up -d --remove-orphans` sem `--profile obs` deixa esses containers como estão. Eles voltam sozinhos depois de um reboot (`restart: unless-stopped`).

## Ligar em dev

Na raiz do repositório, com `METRICS_TOKEN` e `GRAFANA_ADMIN_PASSWORD` preenchidos no `.env`:

```bash
docker compose -f docker/compose.yaml --profile dev --profile app up -d api     # o api só lê METRICS_TOKEN ao ser recriado
docker compose --env-file .env -f docker/compose.yaml --profile obs up -d
```

O `--env-file .env` é necessário: sem ele o Compose procura `docker/.env` e falha com `environment variable "METRICS_TOKEN" ... is not set`. Em dev, o `OTEL_EXPORTER_OTLP_ENDPOINT` é `http://otel-collector:4318` para o app em container e `http://127.0.0.1:8143` para o app rodando no host.

## Desligar

```bash
$C --profile obs stop otel-collector prometheus grafana       # mantém o histórico
$C --profile obs rm -sf otel-collector prometheus grafana     # remove os containers; os volumes ficam
```

**Não use `down`:** o projeto Compose é o mesmo do app. Com a máquina apertada, pare só o `grafana`: a coleta e os alertas continuam.

Em Windows com Git Bash, prefixe os comandos `exec` com `MSYS_NO_PATHCONV=1`, senão o shell reescreve os caminhos.

## Painel "Agent Platform: operação"

Como ler: tudo vem de `/metrics`. Os valores "(1h)", "(24h)" e "(30d)" são janelas móveis calculadas no banco, não contadores: não use `rate()` neles. Nos números de "Agora", **"sem dados" significa que o Prometheus não está coletando** (abra o outro painel); nos gráficos, vazio costuma significar que não há nada pendente. Exceção: se uma consulta SQL de `/metrics` quebrar, aquela métrica some em silêncio e o painel mostra 0 ou vazio. Na dúvida, `$C logs --since 1h api | grep "metric .* failed"`.

| Painel | O que significa | Quando agir |
| --- | --- | --- |
| Kill switches | Pausas ativas, em qualquer escopo | Acima de 0 sem incidente em curso: alguém esqueceu ligado ([kill switch](kill-switch.md)) |
| Tokens inválidos | Canais pausados por token inválido | Acima de 0 ([token](token-meta-invalido.md)) |
| Dead letters (1h) | Falhas de negócio na última hora | A partir de 1 ([fila](fila-travada-dlq.md)) |
| Jobs falhos (total) | Total em `failed`; só cai quando alguém reprocessa ou remove | Crescendo. Um número alto e parado é passivo a limpar, não incidente |
| Spool de webhooks | Bytes aguardando replay | A partir de 1 MB ([spool](spool-crescendo.md)) |
| Maior orçamento | O escopo vivo mais cheio; dia antigo não conta | 80% avisa, 100% degrada ([orçamento](orcamento-esgotado.md)) |
| Evento de entrada mais antigo sem processar | Idade do mais antigo em `pending`/`processing` | Acima da linha de 5 min |
| Eventos de entrada sem processar | Quantos estão nesse estado | Subindo junto com a idade |
| Item mais antigo no outbox | Idade do mais antigo em `pending`/`sending`/`unknown` já liberado | Acima da linha de 10 min |
| Outbox por status (24h) | Itens criados em 24 h. `cancelled` por janela ou kill switch é esperado | `unknown` persistente: nunca reenvie à mão |
| Turnos por status (1h) | Turnos iniciados na última hora | 5 ou mais `failed` |
| p95 da duração do turno (1h) | p95 da última hora, reage devagar | Acima da linha de 20 s por 10 min |
| Custo de LLM hoje (UTC) | Soma desde 00:00 UTC (21:00 em São Paulo); `platform` = sem tenant | Inclinação fora do normal |
| Chamadas de LLM sem sucesso (1h) | Status diferente de `ok`, inclusive negadas por orçamento | Junto com turnos falhando ou handoff em alta |
| Uso de orçamento por escopo | Tabela do escopo mais cheio para o menos cheio. `key:*` = chave do provedor; `total` e `day:*` = tenant | Como acima |
| Handoffs abertos | Nenhum humano assumiu ainda | Crescendo: fale com o vendedor |
| Handoffs fora do SLA | Abertos e vencidos | Acima de 0: confira se o vendedor recebeu o aviso |
| Filas: jobs em todo e doing | `todo` crescendo sem `doing` = worker parado | [Fila](fila-travada-dlq.md) |
| Problemas em revisão (30d) | Veredito `problem` por categoria | Revisão de qualidade, não incidente |

## Painel "Agent Platform: saúde da observabilidade"

| Painel | O que significa |
| --- | --- |
| Alvos | Última coleta de cada alvo. `agentplatform-api` fora = o outro painel e os alertas de cima estão cegos |
| Alertas disparando agora | Regras em `firing` neste Prometheus |
| Duração da coleta de `/metrics` | Subindo, o Postgres está sob pressão. A linha de 15 s é o timeout: passou disso, a coleta falha |
| Linhas de `/metrics` por coleta | `respondidas` = o que o app devolve; `guardadas` = o que fica depois de descartar os escopos de orçamento por turno e conversa. `respondidas` sobe para sempre enquanto essas linhas não forem retiradas de `/metrics` |
| Memória residente | Prometheus e collector. O Grafana não é coletado: `docker stats` |
| Séries ativas no Prometheus | Um salto indica label com valores demais |
| Tamanho dos blocos no disco | Retenção de 15 dias ou 1 GB, o que vier primeiro |
| Spans recebidos e recusados | `recusados` acima de 0 = `memory_limiter` atuando |
| p95 por tipo de span | `llm.chat`, `channel.send`, `job.*`, janela de 5 min. Mostra onde o turno gasta tempo |
| Spans com erro por tipo | Por segundo, com status de erro |

## Alertas: o que o app não consegue dizer

### AgentMetricsTargetDown

Crítico. O Prometheus não lê `/metrics` há 5 min, então os outros alertas daqui estão mudos.

```bash
curl -s http://127.0.0.1:8190/api/v1/targets | grep -o '"lastError":"[^"]*"' | head -1
$C ps api postgres
```

| `lastError` | Causa | Ação |
| --- | --- | --- |
| `404 Not Found` | `METRICS_TOKEN` vazio no `api` ou diferente nos dois lados: o endpoint responde 404 nos dois casos, de propósito | Produção: ambos leem `secrets/metrics_token`. Dev: preencha o `.env`. Nos dois, recrie `api` e `prometheus` (`up -d --force-recreate --no-deps`) |
| `500` | `/metrics` consulta o banco; Postgres fora ou recusando | [Postgres fora](postgres-redis-fora.md) |
| `connection refused`, `no such host` | `api` parado | `$C logs --tail 50 api`; [deploy e rollback](deploy-e-rollback.md) se for versão nova |
| `context deadline exceeded` | Coleta acima de 15 s; banco sob pressão | Veja "Duração da coleta" e os logs do Postgres |

Verificação: em "Alvos", `agentplatform-api` volta a "no ar" em até 1 min.

### AgentMetricsTargetMissing

Crítico. O job `agentplatform-api` não existe na configuração carregada: alguém editou `docker/obs/prometheus/prometheus.yml`. Corrija, valide com `$C --profile obs exec prometheus promtool check config /etc/prometheus/prometheus.yml` e rode `$C --profile obs restart prometheus`.

### MaintenanceQueueNotConsumed

Crítico. Há job parado em `todo` na fila `maintenance` há mais de 10 min. Só o `worker-bg` consome essa fila, e é nela que rodam os alertas do app, o heartbeat, os follow-ups e a retenção: sem ele, o app fica mudo sem saber. Normalmente um job sai de `todo` em segundos.

```bash
$C ps worker-bg                                   # parado ou reiniciando (OOM)?
$C logs --since 30m worker-bg | tail -50
```

Parado: `$C up -d worker-bg`. No ar, mas sem consumir: as duas vagas podem estar ocupadas por `ingest` ou `lab` longos; veja [fila travada](fila-travada-dlq.md). Verificação: o alerta some em até 10 min e os avisos do app voltam.

### OtelCollectorDown

Aviso. Spans se perdem; o atendimento não é afetado (o app só registra a falha de exportação). `$C --profile obs logs --tail 50 otel-collector` e `$C --profile obs up -d otel-collector`. O container com pouca memória aparece como reinício em `$C --profile obs ps`.

### OtelCollectorRefusingSpans

Aviso. O `memory_limiter` está recusando dados: os painéis de tracing ficam incompletos, o atendimento segue. Se durar, veja se há pico real de turnos; caso contrário, desligue o tracing (esvazie `OTEL_EXPORTER_OTLP_ENDPOINT` e recrie o app) antes de dar mais memória ao collector, porque a VPS não tem folga.

### PrometheusRuleEvaluationFailing

Aviso. Uma regra está falhando e o alerta dela está mudo. Em `http://127.0.0.1:8190/rules`, procure o erro; corrija em `docker/obs/prometheus/rules/agentplatform.yml`, valide com `promtool test rules /etc/prometheus/tests/agentplatform.test.yml` e reinicie o `prometheus`.

## Alertas: espelho do que o app já avisa

Estes repetem os limites do app **de fora dele**: os alertas do app rodam no `worker-bg` e param junto com ele. Os números são os padrões de `OpsAlertsConfig` e do plano `10`. O Prometheus não lê a config do tenant: se um limite mudar lá, mude em `rules/agentplatform.yml`.

| Alerta | Dispara quando | Severidade | O que fazer |
| --- | --- | --- | --- |
| InboundBacklog | Evento sem processar há mais de 5 min | crítico | [Fila travada](fila-travada-dlq.md) |
| OutboxStuck | Item parado no outbox há mais de 10 min | crítico | [Fila travada](fila-travada-dlq.md); não reenvie à mão |
| TurnFailures | 5 ou mais turnos `failed` na última hora | aviso | [Fila travada](fila-travada-dlq.md) |
| DeadLetters | 1 ou mais dead letters na última hora | aviso | [Fila travada](fila-travada-dlq.md) |
| JobsFailed | Mais jobs `failed` numa fila do que há 1 h | aviso | [Fila travada](fila-travada-dlq.md) |
| SpoolGrowing | Spool com 1 MB ou mais | crítico | [Spool](spool-crescendo.md); nunca apague o spool |
| BudgetHigh | Escopo vivo entre 80% e 100% | aviso | [Orçamento](orcamento-esgotado.md) |
| BudgetExhausted | Escopo vivo em 100% ou mais | crítico | [Orçamento](orcamento-esgotado.md) |
| ChannelTokenInvalid | Conta de canal com token inválido | crítico | [Token da Meta](token-meta-invalido.md) |

### TurnLatencyHigh

Aviso. p95 da última hora acima de 20 s por 10 min (plano `10`). Com tracing ligado, "p95 por tipo de span" mostra se o tempo está em `llm.chat` (provedor lento: veja o modelo e `llm_calls`) ou em `job.*` (fila ou banco). Sem tracing, consulte o banco:

```bash
$C exec postgres psql -U agentplatform -d agentplatform -c "select node, model_used, count(*), percentile_cont(0.95) within group (order by latency_ms) as p95_ms from llm_calls where created_at > now() - interval '1 hour' group by 1, 2 order by p95_ms desc nulls last"
```

Mais lento não é mais errado: só atue se o cliente estiver esperando demais ou se aparecerem turnos `failed`.

Só o app sabe, sem métrica que o Prometheus possa ler: `no_inbound`, `handoff_undelivered`, `standby`, `channel_error`, `provider_out_of_credit`, `wal_archive`, `restore_test_*`, `unreadable_webhooks`. Máquina desligada ou banco fora é o `HEARTBEAT_URL` que pega, de fora.

## Mudar alguma coisa

- Dashboards: edite o JSON em `docker/obs/grafana/dashboards/`; o Grafana relê em até 1 min. A interface não salva mudanças por cima.
- Regras e `prometheus.yml`: valide **antes** de reiniciar, porque com configuração inválida o Prometheus não sobe.
  ```bash
  $C --profile obs exec prometheus promtool check config /etc/prometheus/prometheus.yml
  $C --profile obs exec prometheus promtool test rules /etc/prometheus/tests/agentplatform.test.yml
  $C --profile obs restart prometheus
  ```
- Métrica nova só entra em painel ou regra depois de existir em `collect_metrics` (`core/ops/observability.py`).
- Senha do Grafana: o arquivo (ou `GRAFANA_ADMIN_PASSWORD`) só vale no primeiro início. Depois, troque o arquivo e aplique com `$C --profile obs exec grafana grafana cli admin reset-admin-password "$(cat secrets/grafana_admin_password)"`.
- Imagens: versão exata no Compose, nunca `latest`. Ao trocar o collector, confira os nomes das métricas: nesta versão é `otelcol_receiver_refused_spans`, sem `_total`, e um nome errado deixa a regra muda sem erro nenhum.
