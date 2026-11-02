# Agent Platform

Plataforma multi-tenant e multi-segmento de agentes de atendimento e vendas por mensagem. Primeiro tenant: consultor de
consórcio no Instagram. O plano de arquitetura, sem o que é de clientes (ADR-023), está em [`docs/plan/`](docs/plan/00-resumo-executivo.md), e o
estado da implementação, em [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

## Rodar em desenvolvimento

Pré-requisitos: Docker, Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # preencha as chaves (nunca faça commit do .env)
python -m uv sync                 # dependências
docker compose -f docker/compose.yaml --profile dev up -d     # Postgres (55432) e Redis (56379)
.venv/Scripts/python -m agentplatform.cli migrate             # Alembic + procrastinate + checkpointer
.venv/Scripts/python -m agentplatform.cli bootstrap           # tenant dev-consortium + usuário do console
.venv/Scripts/python -m agentplatform.cli ingest              # traz docs/materiais/ para revisão no console; não aprova nem chama provedor
```

## Conversar com o agente (pipeline completo, canal falso)

```bash
.venv/Scripts/python -m agentplatform.cli simulate                       # interativo
.venv/Scripts/python -m agentplatform.cli simulate --say "oi" "quero um carro de 80 mil"
.venv/Scripts/python -m agentplatform.cli budget                         # quanto já foi gasto da chave
```

Cada turno custa frações de centavo e conta contra `LLM_TOTAL_CAP_USD`. O `ModelGateway` recusa qualquer chamada
acima do teto.

## Testes e gate de avaliação

```bash
.venv/Scripts/python -m pytest                                # unit + contract + integration + chaos (sem custo)
# os testes usam o banco agentplatform_test (criado e migrado sozinho), nunca o banco do app
.venv/Scripts/python -m pytest -m live                        # uma chamada real mínima
.venv/Scripts/python -m agentplatform.cli lab gate --level fast    # determinístico, todo PR
.venv/Scripts/python -m agentplatform.cli lab gate --level full --verbose   # ao vivo (~US$ 0,02)
```

## Subir API e workers

```bash
docker compose -f docker/compose.yaml --profile dev --profile app up -d --build
# API em http://127.0.0.1:8100 (webhook: /webhooks/meta, console: /console/)
```

## Console (painel do vendedor e do operador)

Com a API no ar, acesse `http://127.0.0.1:8100/console/` (o `bootstrap` mostra a senha gerada uma única vez, ou use `--seller-password`; troque depois em Trocar senha).

| Tela | O que faz |
| --- | --- |
| Preparação | O que falta para o assistente atender de verdade, lido do estado real |
| Conversas | Estado, resumo, por que respondeu cada mensagem, assumir/devolver ao bot |
| Assistente / Regras / Modelos | Nome, tom, instruções, mensagens, horários, diretrizes, modelos: viram um rascunho, sem editar YAML |
| Testar a base | O que a busca acha para uma pergunta, e por quê, com ajuste de rigor |
| Curadoria / Base de conhecimento / Perguntas frequentes | Perguntas sem base, material com revisão e aprovação, conflitos, troca do modelo de busca |
| Config | O que cada rascunho muda, avaliação por botão, outra pessoa aprova; canário por % das conversas; rollback; YAML como opção avançada |
| Usuários | Convite por link, papel, desativar |
| Orçamento | Gasto por escopo e por dia, recarga (passa pela aprovação de config) |
| Dashboards | Funil, atribuição por origem, variantes de resposta privada, SLA de handoff, custo |
| Importar leads | CSV/XLSX → mapeamento → dry-run → commit → desfazer lote |
| Tabelas | Tabelas do segmento (ex.: planos e preços): cadastrar/editar na tela ou importar planilha → rascunho com prévia (novas/alteradas/removidas) → ativar → voltar a qualquer versão. O simulador usa o rascunho antes de ativar. O uso pelo assistente é um feature flag (rascunho de config) |
| Campanhas | Métricas por variante (enviados, entregues, lidos, falhas, opt-outs, respostas) |
| LGPD | Busca do titular, exportação JSON, eliminação com confirmação digitada |
| Canais | Status do token e reconexão via Facebook Login |
| 2FA | TOTP (obrigatório para os papéis em `CONSOLE_TOTP_REQUIRED_ROLES`) |

## Operação

- `GET /metrics` (Prometheus, exige `Authorization: Bearer $METRICS_TOKEN`): fila, outbox, turnos (p95), custo do dia,
  orçamento por escopo, handoffs e SLA, dead letters, tokens inválidos, jobs.
- Alertas ao operador a cada minuto (limiares em `operations.alerts` da config): mensagens sem processar, outbox
  parado, turnos falhando, nenhum webhook, token inválido, orçamento em 50/80/100%, spool, jobs falhos. Envio por
  e-mail (`OPS_ALERT_EMAIL`) e/ou WhatsApp (`OPS_ALERT_WHATSAPP`, template `ops_alert_v1`); sem canal, vão para o log.
- Tracing OpenTelemetry: defina `OTEL_EXPORTER_OTLP_ENDPOINT` (sem PII nos atributos).
- Runbooks em `docs/runbooks/`.

## Produção (VPS dedicada)

`docker/compose.prod.yaml`: Caddy com HTTPS automático (só `/webhooks/*`, `/f/*`, `/console/*` e `/health` expostos),
Postgres e Redis em rede interna, segredos em arquivos (`docker/secrets/`), app com sistema de arquivos somente
leitura, limites para 2 vCPU / 4 GB e backup contínuo com wal-g (WAL cifrado + base diária).

```bash
cp docker/prod.env.example docker/prod.env          # ajuste domínio, e-mails, destino S3 do backup
scripts/deploy/init-secrets.sh                      # gera senhas/KEK/pepper; preencha as chaves de terceiros
scripts/deploy/deploy.sh ghcr.io/<org>/agentplatform:<sha>   # migra, sobe e espera o /health
scripts/deploy/deploy.sh --rollback                 # volta à imagem anterior
scripts/backup/restore_test.sh                      # restaura o último backup num container descartável e confere
```

Antes do primeiro deploy: CI verde (`.github/workflows/ci.yml`), `pre-commit install` e os runbooks em `docs/runbooks/`.

## Provedor de LLM

- `LLM_PROVIDER=openrouter` (padrão para dados reais): ZDR e `data_collection=deny` em todos os modelos.
- `LLM_PROVIDER=openai`: OpenAI direta, **só com dados sintéticos** (sem ZDR por padrão).

A config do tenant (`config/tenants/*.yaml`) escolhe o modelo por nó, e o provedor resolve o roteamento.
