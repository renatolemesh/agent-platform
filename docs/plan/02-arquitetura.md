# 02 — Arquitetura

Mensagens entram por adaptadores de canal, viram eventos canônicos persistidos, passam por debounce e por execução serializada por conversa, rodam o grafo do agente e saem **só** pelo outbox, cujo despachante checa posse, janela, horário e limites antes de cada envio.

## 1. C4 — Contexto

```mermaid
flowchart LR
  Lead(["Lead / cliente<br/>(Instagram)"])
  Seller(["Vendedor<br/>(app do Instagram + WhatsApp)"])
  Op(["Operador da plataforma"])
  subgraph Platform["Plataforma de agentes (nosso sistema)"]
    SYS["Agent Platform"]
  end
  Meta["Meta Graph API<br/>(Instagram, webhooks)"]
  WA["WhatsApp Cloud API<br/>(notificador do vendedor)"]
  OR["OpenRouter<br/>(LLMs, embeddings, STT)"]
  SMTP["E-mail transacional<br/>(fallback do aviso)"]
  S3["Object storage<br/>(backups)"]
  Lead -- comenta / conversa --> Meta
  Seller -- responde pelo app --> Meta
  Meta -- webhooks --> SYS
  SYS -- respostas, private replies --> Meta
  SYS -- aviso de handoff --> WA
  WA -- botões assumi/devolver --> SYS
  WA --> Seller
  SYS --> OR
  SYS --> SMTP
  SYS --> S3
  Seller -- console --> SYS
  Op -- console / admin API --> SYS
```

## 2. C4 — Containers

```mermaid
flowchart TB
  subgraph Edge
    CADDY["caddy<br/>TLS, proxy"]
  end
  subgraph App["app image (Python 3.12)"]
    API["api<br/>FastAPI: webhooks (fast-ack), Admin API, console Jinja/HTMX"]
    W["worker<br/>filas turns, comments, media, dispatch, echo"]
    WBG["worker-bg<br/>followups, ingest, lab, maintenance + periódicos"]
  end
  PG[("postgres 17 + pgvector<br/>negócio, outbox, fila procrastinate,<br/>checkpoints LangGraph, vetores, config")]
  RD[("redis<br/>debounce, rate limit, cache, pub/sub cancel")]
  OBS["otel-collector → prometheus/loki/grafana"]
  CADDY --> API
  API --> PG
  API --> RD
  W --> PG
  W --> RD
  WBG --> PG
  WBG --> RD
  W -- HTTPS --> META["Meta Graph API"]
  W -- HTTPS --> ORT["OpenRouter"]
  WBG -- HTTPS --> ORT
  W -- HTTPS --> WAC["WhatsApp Cloud API"]
  API -. OTLP .-> OBS
  W -. OTLP .-> OBS
  WBG -. OTLP .-> OBS
```

## 3. C4 — Componentes (dentro da imagem `app`)

```mermaid
flowchart LR
  subgraph core["agentplatform.core (agnóstico de segmento)"]
    GW["channels.gateway<br/>verify, normalize, persist"]
    CH["channels.ports<br/>ChannelAdapter, HumanInboxPort, Notifier"]
    IG["channels.instagram<br/>InstagramGraphAdapter"]
    WAN["channels.whatsapp_notifier"]
    DEB["orchestration.debouncer"]
    ORC["orchestration.orchestrator<br/>run_epoch, posse, config"]
    HD["orchestration.human_detector"]
    AG["agent.graph<br/>LangGraph: nós do 04"]
    GU["agent.guards"]
    CTX["agent.context_builder"]
    TL["tools.registry + executor"]
    KB["knowledge.service<br/>FAQ + RAG híbrido"]
    ING["knowledge.ingest"]
    MG["models.gateway<br/>OpenRouter + budget"]
    OB["outbox.dispatcher"]
    CMT["comments.automation"]
    FU["followups.engine"]
    CFG["config.registry"]
    PK["packages.loader"]
    ADM["admin.api + console"]
    LAB["lab.runner"]
  end
  subgraph pkgs["packages (plugins)"]
    CONS["consortium"]
    AUTO["auto_insurance (teste)"]
  end
  GW --> DEB --> ORC --> AG
  GW --> HD --> ORC
  GW --> CMT --> OB
  AG --> CTX
  AG --> GU
  AG --> TL
  AG --> KB
  AG --> MG
  AG --> OB
  FU --> OB
  OB --> CH
  CH --> IG
  CH --> WAN
  ORC --> CFG
  CFG --> PK
  PK --> CONS
  PK --> AUTO
  TL --> PK
  ADM --> CFG
  ADM --> ING --> KB
  LAB --> AG
```

## 4. Fluxo de referência

```mermaid
flowchart LR
  IG["Instagram (Graph API)"] -->|webhook| GW["Channel Gateway"]
  GW --> INB[("inbound_events")]
  INB -->|mensagens| DEB["Debouncer"]
  INB -->|comentários| CMT["Automação de comentários"]
  INB -->|ecos| HD["Detector de humano"]
  DEB --> ORC["Orquestrador: fila com lock por conversa, run_epoch, posse, config"]
  HD --> ORC
  ORC --> AG["Grafo LangGraph (checkpoint Postgres)"]
  AG --> TL["Ferramentas núcleo + pacote"]
  AG --> KB["Knowledge Service"]
  AG --> MG["ModelGateway + orçamento"]
  AG --> HIP["HumanInboxPort → WhatsApp/e-mail"]
  AG --> OUT[("outbox")]
  SCH["Scheduler"] --> OUT
  CMT --> OUT
  OUT --> DSP["Despachante: kill switch, epoch, posse, opt-out, janela, horário, staleness, rate limit"]
  DSP --> IG
  ADM["Admin API + console"] --> CFG[("config versionada + pacotes")]
  CFG -.-> ORC
  CFG -.-> AG
```

## 5. Desvios em relação à arquitetura de referência do prompt

| Ponto | Referência | Este plano | Por quê | ADR |
| --- | --- | --- | --- | --- |
| `understand_turn` + `classify_stage` | Dois nós paralelos | Um nó `analyze_turn` com duas saídas (modo `merged`); `parallel` disponível por config | Orçamento e latência; o laboratório decide | ADR-017 |
| Fila | Em aberto | procrastinate (Postgres), com `lock=conversation_id` | Serialização por chave nativa, zero RAM extra, durável | ADR-004 |
| Lock | TTL + fencing (Redis) ou advisory | Fila serializada + `run_epoch` como fencing no Postgres + `pg_advisory_xact_lock` no commit | Correção sem depender do Redis | ADR-005 |
| Tracing | Langfuse (Cloud ou self-hosted) ou OTel | OTel + tabelas no Postgres; Langfuse Cloud opcional com amostragem | Self-hosted pede 16 GiB; Cloud Hobby não cobre o volume sem amostragem | ADR-007 |
| Notificador | Telegram, e-mail ou outro | WhatsApp Cloud API (decisão do usuário) + e-mail de fallback | Decisão do usuário | ADR-010 |
| Login Meta | Instagram Login (token de 60 dias) | Facebook Login (Page token sem expiração fixa) | Situação real do app | ADR-001 |
| Console | Não especificado | FastAPI + Jinja2 + HTMX (sem SPA) | Mantém uma linguagem só; evita ADR de TypeScript | ADR-003 |

## 6. Escala

- **100 → 1.000 conversas/dia:** `--scale worker=3`, Postgres com mais memória; nenhuma mudança de código (fila particionada por conversa, sem estado em memória).
- **Mais tenants:** RLS + config por tenant; filas compartilhadas com fairness por tenant (`priority` + limite de concorrência por tenant no orquestrador).
- **Mais canais:** novo `ChannelAdapter` + novos tipos de evento canônico, se preciso.
- **Pontos de atenção:** Postgres como fila + dados + vetores na mesma instância. Gatilho para separar: `queue_lag` p95 > 5 s com CPU do Postgres > 70% → réplica de leitura para busca vetorial ou Postgres dedicado à fila.
