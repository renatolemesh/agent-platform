# Teste de carga do pipeline

Script: `tests/load/load_pipeline.py` (fora da coleta do pytest). Roda o **pipeline real**: `ingress` → debounce (`close_batch`) → `run_turn` com o checkpointer do LangGraph → outbox → despachante, contra o Postgres de dev. Usa `FakeChannelAdapter` e `ScriptedGateway`, então **não gasta LLM** e não chama a rede. Um agendador em processo emula do procrastinate o que importa: W slots de worker, `schedule_in` em tempo real, `queueing_lock` e `lock` por conversa.

Cada conversa manda uma rajada de 3 mensagens (intervalos de 0 a 150 ms, dentro da janela de debounce de 0,4 s), espera a resposta e manda mais 1. Invariantes conferidos (o script sai com código 1 se algum falhar):

- exatamente uma resposta por rajada e nenhum envio duplicado;
- exatamente um `run_turn` por lote de debounce (2 por conversa);
- nenhuma mensagem de entrada perdida (todas consumidas por um turno) e todo o outbox em `sent`.

```bash
docker compose -f docker/compose.yaml --profile dev up -d
.venv/Scripts/python tests/load/load_pipeline.py --conversations 50 --workers 8
```

## Resultados (2026-10-10, notebook Windows 11, Postgres 17 no Docker Desktop, 1 processo)

| Conversas | Workers | Mensagens recebidas | Respostas | Parede | Respostas/s | 1ª resposta p50 / p95 | 2ª resposta p50 / p95 | `run_turn` p50 / p95 | Invariantes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5 | 4 | 20 | 10 | 6,7 s | 1,5 | 1,08 / 1,22 s | 1,01 / 1,11 s | 0,13 / 0,22 s | ok |
| 50 | 8 | 200 | 100 | 11,0 s | 9,1 | 4,51 / 5,13 s | 3,87 / 4,24 s | 0,64 / 0,71 s | ok |
| 100 | 8 | 400 | 200 | 24,4 s | 8,2 | 9,14 / 9,88 s | 7,58 / 8,55 s | 0,66 / 0,75 s | ok |

Contagem de jobs com 50 conversas: 200 `ingress`, 244 `close_batch` (os 44 extras são re-checagens do debounce quando a rajada ainda não fechou), 100 `run_turn`, 100 `dispatch`, 50 `summarize`. Turnos: 100 `committed`. Outbox: 100 `sent`.

## Leitura

- **Correção sob concorrência:** sem duplicidade, sem perda, um turno por rajada, inclusive com 100 conversas simultâneas.
- **Teto de um processo:** com o LLM simulado (resposta instantânea), o gargalo é CPU de um processo Python (grafo + guards + serialização do checkpoint): cerca de 8–9 respostas/s. O `run_turn` sobe de 0,13 s (sem disputa) para 0,65 s com 8 slots, e a latência ponta a ponta cresce por fila, não por erro.
- **Em produção** cada turno espera de 2 a 8 s pelo LLM (I/O), então 8 slots por processo ficam ocupados esperando rede e a vazão real é limitada pelo provedor e pelo orçamento, não pela CPU. Para o piloto (1 consultor, dezenas de conversas por dia), um `worker` com `--concurrency=8` tem folga de duas ordens de grandeza.
- **Escalar:** `docker compose up -d --scale worker=2` (nenhum estado em memória; o `lock` por conversa vale entre processos). Com mais de 2 processos, suba o `max_connections` (60) ou reduza o pool por processo (10).
- O tempo de parede inclui os 5 s do `summarize` agendado depois do último turno.
