# 08 — Harness: configuração, orçamento, custo e segurança

O harness é tudo em volta do modelo: runtime, montagem de contexto, ferramentas, políticas, gateway de modelos, config e avaliação. Tudo nele é **parâmetro versionado, validado por schema e auditável**.

## 1. Registro de configuração

- Documento por tenant e ambiente em `config_versions` (`03`), validado pelo `PlatformConfig` (Pydantic v2, `extra="forbid"`).
- **Schema completo e validável:** [`anexos/config_schema.py`](anexos/config_schema.py), exemplo do 1º tenant em [`anexos/config.example.yaml`](anexos/config.example.yaml), JSON Schema exportado em [`anexos/config.schema.json`](anexos/config.schema.json). `python anexos/validate_config.py` valida o exemplo e prova que são **rejeitados**: compliance mais permissivo que o do pacote, juiz da mesma família do modelo de resposta, teto diário acima da trava do OpenRouter, `data_collection: allow`, regra de comentário com uma única variante pública, chave desconhecida, fallback do juiz da mesma família, teto do piloto acima do crédito líquido, embedding sem ZDR, divulgação obrigatória esvaziada e go-live com transferência internacional pendente (resultado em 09/10/2026: 13/13 OK).
- **Resolução:** `defaults da plataforma ⊕ pacote@versão ⊕ override do tenant ⊕ variante do experimento (lab)`. O merge é profundo, e a monotonicidade de compliance é verificada (`13` §1).
- **Fluxo de mudança:** rascunho → diff no console → gate de avaliação (`10`) → aprovação (prompts e compliance: **dois papéis**, operador da plataforma e dono do tenant) → canário (`canary_percent` das conversas novas, por hash de `conversation_id`) → ativa → rollback em 1 clique (reativa a versão anterior).
- **Hot reload:** o orquestrador lê `cfg:active:{tenant}` (cache de 30 s) no início de cada turno; o turno grava `config_version_id` e roda inteiro com ela (reprodutível).
- **Kill switches** em tabela própria, com cache de 5 s, e auditados.

## 2. O que é parâmetro (mapa para o schema)

| Grupo | Chave no schema | Exemplos |
| --- | --- | --- |
| Modelos | `models.*` | Modelo e fallbacks por nó, temperatura, max tokens, timeout, esforço de raciocínio, versão de prompt, política de provedor (ZDR, `data_collection=deny`, `require_parameters`) |
| Conversa | `conversation.*` | Persona, tom, mensagens de espera e de erro, contexto máximo, atraso entre bolhas |
| Agente | `agent.*` | `analyze_mode`, chamadas de LLM por turno, timeout do turno |
| Funil | `funnel.*` | Limiares de estágio e slot, turnos sem progresso |
| Diretrizes | `guidelines.*` | Máximo por turno, overrides |
| Ferramentas | `tools.{name}` | Habilitação, estágios, timeout |
| RAG | `rag.*` | Índice ativo, limiares, top-k, RRF, rerank |
| Concorrência | `concurrency.*` | Debounce, política de mensagem nova, heartbeat, staleness, eco |
| Handoff | `handoff.*` | Gatilhos, roteamento, SLA, mensagens, notificador |
| Envio | `sending.*` | Horário silencioso, feriados, teto de frequência, margem de rate limit |
| Comentários | `comments.*` | Regras, variantes, janela de dedup |
| Follow-ups | `followups.*` | Cadências |
| Compliance | `compliance.*` | Frases proibidas, divulgações, `never_request`, palavras de opt-out |
| Mídia | `media.*` | Transcrição, tratamento de imagem e documento (OCR local mascarado por padrão), retenção da mídia bruta |
| Guards | `input_guard.*`, `output_guard.*` | Tamanho máximo, limiares de opt-out e injection, taxa por contato, n-grama de citação, números com fonte |
| LGPD | `subprocessors[]` | Inventário de operadores e suboperadores, com país, mecanismo de transferência e ZDR; bloqueia o go-live com pendência |
| Operação | `operations.*` | Feature flags, canário, amostragem de trace, retenção |
| Pacote | `segment_package` | Nome e versão |
| Orçamento | `budget.*` | Teto total, diário, por conversa e por turno; alertas; ordem de degradação; teto de experimento |

## 3. ModelGateway e orçamento (ADR-011)

```python
class ModelGateway:
    async def call(self, node: str, req: LlmRequest, ctx: CallContext) -> LlmResponse:
        spec = ctx.config.models[node]
        # worst case of the chain: counted input tokens + max_output, priced at the MOST expensive model/endpoint allowed
        est = self.estimate_cost(spec, req, price="max_of_chain")
        reservation = await self.budget.reserve(ctx.tenant_id, scopes=ctx.budget_scopes, amount=est,
                                                ttl_s=ctx.config.budget.reservation_ttl_s)
        if reservation.denied:
            raise BudgetDenied(level=reservation.level)           # graph degrades (07 §8.7)
        resp: LlmResponse | None = None
        try:
            resp = await self.openrouter.chat(
                models=[spec.primary, *spec.fallbacks],
                provider=spec.provider.model_dump(), response_format=..., timeout=spec.timeout_s,
                reasoning={"effort": spec.reasoning_effort}, user=ctx.pseudonymous_user_id)
            output_model.model_validate_json(resp.content)       # always validated on our side
            return resp
        finally:
            if resp is not None and resp.usage and resp.usage.cost is not None:
                await self.budget.settle(reservation, actual=resp.usage.cost)
            else:
                # timeout / error / missing usage: settle at the ESTIMATE (never 0) and reconcile later via
                # GET /api/v1/generation?id=... (review #13)
                await self.budget.settle(reservation, actual=est, needs_reconcile=True)
            await self.llm_calls.record(node=node, resp=resp, reservation=reservation)
```

- **Reserva/liquidação** em `budget_balances` (`03`): `update budget_balances set reserved = reserved + :est where scope = :s and spent + reserved + :est <= cap` (atômico, um `update` por escopo na mesma transação). O gasto paralelo não estoura o teto. Cada reserva tem TTL (`budget.reservation_ttl_s`); um sweeper libera reservas de workers mortos. `budget_ledger` guarda o histórico.
- **Dia em UTC** (`budget.day_boundary_tz = UTC`), o mesmo corte do `limit_reset: daily` do OpenRouter. Os dashboards mostram também o dia de Brasília, mas o teto conta em UTC (revisão #13).
- **Teto total abaixo do crédito líquido:** com US$ 5 comprados, sobram ≈ US$ 4,74 depois da taxa de 5,5%, e o teto do piloto é US$ 4,50. Assim, a degradação começa antes do HTTP 402 do OpenRouter (o schema obriga).
- **Escopos:** `total` (piloto), `day`, `conversation:<id>`, `turn:<id>`, `experiment:<id>`, `ci:<data>`.
- **Alertas** em 50%, 80% e 100% (notificador do operador).
- **Segunda trava no OpenRouter:** chave de produção com `limit` e `limit_reset: daily` (ou guardrail com orçamento diário), sempre **acima** do teto do harness (o schema obriga), para a degradação acontecer antes do corte. Chave separada (e limite próprio) para o laboratório.
- **Conferência:** job horário compara `Σ llm_calls.cost_usd` com `GET /api/v1/key` (`usage_daily`) e alerta se a diferença passar de 5%.
- **Degradação** (nunca silêncio): (1) modelo mais barato da cadeia → (2) modo só FAQ (FTS sem LLM nem embedding) → (3) handoff + mensagem curta.
- **Recarga:** o operador recarrega créditos no OpenRouter e ajusta `budget.*` pelo console (auditado). Premissa P2: recarga conforme o uso, teto diário de referência US$ 2.

## 4. Custo

Preços do OpenRouter consultados em 09/10/2026 (`18-fontes-externas.md`), em US$ por 1M tokens. Premissa de volume: **100 conversas/dia ≈ 800 turnos/dia**.

**Tokens por turno (contexto mínimo do `04` §4):**

| Chamada | Frequência | Entrada | Saída |
| --- | --- | --- | --- |
| `analyze_turn` | todo turno | 2.500 | 250 |
| `respond` | todo turno (+~10% de retry do guard) | 4.000 | 300 |
| juiz do `output_guard` | ~20% dos turnos | 1.500 | 100 |
| `summarize` | 1 a cada 5 turnos | 2.000 | 200 |
| embedding | 1 por turno com busca | 50 | — |
| transcrição | 10% dos turnos, 30 s cada | — | — |

**Cenário econômico (padrão do MVP, todos os provedores com ZDR):** `analyze_turn` e `summarize` em `google/gemini-2.5-flash-lite` (0,10/0,40); `respond` em `deepseek/deepseek-v4-flash` via DeepInfra (0,09/0,18); juiz em `openai/gpt-5-nano` (0,05/0,40, outra família); embedding `qwen/qwen3-embedding-8b` (0,01); transcrição `whisper-large-v3-turbo` via `/audio/transcriptions` (≈ US$ 0,0007/min no pior caso).

| Item | Conta por dia | US$/dia |
| --- | --- | --- |
| Modelo pequeno (analyze + summarize) | 2,32M × 0,10 + 0,232M × 0,40 | 0,325 |
| `respond` (+10% retry) | (3,2M × 0,09 + 0,24M × 0,18) × 1,1 | 0,364 |
| Juiz | 0,24M × 0,05 + 0,016M × 0,40 | 0,018 |
| Embeddings | 0,04M × 0,01 | 0,0004 |
| Transcrição | 40 min × 0,00067 | 0,027 |
| **Total** | | **≈ 0,73** |
| + taxa de 5,5% na compra de créditos | | **≈ 0,77** |

- **Por conversa:** ≈ US$ 0,008. **Por lead qualificado** (meta de 30% das conversas): ≈ US$ 0,026.
- **US$ 5 iniciais** (≈ US$ 4,74 após a taxa; teto do harness em US$ 4,50) duram ≈ 6 dias a 100 conversas/dia, ou ≈ 30 dias a 20 conversas/dia (volume provável do piloto). O gate de avaliação no CI tem teto próprio (`budget.ci_daily_cap_usd`, US$ 0,50/dia) e usa a chave do laboratório, então **também consome crédito**: com pass^3 em 20 cenários, cada rodada completa custa ≈ US$ 0,30–0,80, e por isso o gate completo roda no merge para `main` e na publicação de config, e o gate rápido (determinístico + nós) roda em cada PR.
- **Cenário equilibrado** (`respond` em `google/gemini-3.1-flash-lite`, 0,25/1,50; juiz `openai/gpt-5.4-nano`): ≈ US$ 1,58/dia (≈ US$ 0,016 por conversa), ainda abaixo do teto de US$ 2.
- **Cenário qualidade** (`respond` em `gpt-5.4-mini`, juiz `claude-haiku-4.5`): ≈ US$ 4,90/dia; **fora do orçamento** e só justificável se o laboratório mostrar ganho de conversão que pague a diferença.
- **Riscos de custo:** tokens de raciocínio são cobrados como saída (por isso `reasoning_effort: minimal` por padrão); preços variam por endpoint (fixar `provider.order`/`only` e ler `usage.cost` real); o cache de prompt (prefixo estável) pode cortar de 20% a 40% da entrada.
- **Diferença em relação ao prompt:** a saída média real é ≈ 600 tokens/turno (não 400), e o piso realista com ZDR é ≈ US$ 0,70/dia (não US$ 0,40), a menos que se use modelos abertos muito baratos com risco de qualidade em pt-BR.

**Custo fora do LLM (ordem de grandeza):** VPS 2 vCPU/4 GB (≈ US$ 6–12/mês); WhatsApp do notificador (centavos de real por aviso); object storage para backup (< US$ 1/mês); Langfuse Cloud opcional (Hobby grátis, com amostragem).

## 5. Prompts

- Templates versionados por nó (`analyze_turn@v1`, `respond@v1`, ...) em `prompt_templates(name, version, body, variables_schema, eval_run_id, status)`; variáveis tipadas (Pydantic), render com Jinja2 em modo `StrictUndefined`.
- Cada versão só pode ser ativada com `eval_run_id` aprovado. Nenhum prompt em string solta no código (lint: proibido `"""` com mais de 200 caracteres em `src/agent/nodes`).

## 6. Registro de ferramentas

```python
class ToolSpec(BaseModel):
    name: str; description: str
    input_model: type[BaseModel]; output_model: type[BaseModel]
    allowed_stages: list[str] | Literal["*"]
    effect_class: Literal["read", "internal_write", "external"]
    idempotent: bool; timeout_s: float; rate_limit_per_min: int | None
    recipient_from_llm: Literal[False] = False      # never
```

O executor valida a entrada, aplica timeout e rate limit, grava `tool_calls` com `idempotency_key` e devolve resultado compacto. Ferramenta não liberada no estágio → erro devolvido ao modelo ("ferramenta indisponível neste momento").

## 7. Segurança

### 7.1 Threat model (OWASP Top 10 para aplicações LLM, edição 2025)

| Risco | Onde aparece aqui | Mitigação | Teste |
| --- | --- | --- | --- |
| LLM01 Prompt injection (direta e indireta) | Mensagem do cliente; trecho de documento; comentário | Dados delimitados com nonce; regras no system prompt; `input_guard` com score; ferramentas com permissão por estágio e sem destinatário vindo do LLM; números só de ferramenta; guard de saída | Cenários `injection_*` (10 variantes) e red team do promptfoo |
| LLM02 Vazamento de informação sensível | PII no prompt; documentos internos; resumo ao vendedor; imagens de documentos; áudio; checkpoints | Mascaramento antes do LLM; ZDR obrigatório em **todo** modelo (chat, embedding, STT, rerank: `ProviderPolicy` em `ModelRef`); imagens por OCR local mascarado, não por LLM; inventário de subprocessadores na config (`subprocessors`) que bloqueia o go-live com transferência pendente; `guidance_only` sem citação; resumo sem dados sensíveis; traces mascarados; checkpoints cifrados; sem re-hidratação de PII na saída | Grader de PII nos traces; cenários `leak_internal_doc`, `id_photo_sent` |
| LLM03 Cadeia de suprimentos | Bibliotecas, imagens, modelos via OpenRouter | Versões fixadas (`uv.lock`), imagens com digest, `pip-audit` e Trivy no CI; allowlist de modelos e provedores (guardrail do OpenRouter) | CI |
| LLM04 Envenenamento de dados e modelo | Base de conhecimento; FAQs promovidas da curadoria | Só conteúdo `approved` com aprovador; varredura de PII; diff e revisão | Teste de ingestão |
| LLM05 Tratamento inadequado da saída | Texto enviado ao cliente; JSON das ferramentas | Validação Pydantic; quebra por bytes; sem HTML ou markdown no IG; URL só de allowlist | Unit + guard |
| LLM06 Agência excessiva | Ferramentas | Menor privilégio; classe de efeito; nenhuma ferramenta externa no MVP; outbox com checagem final | Teste de permissão por estágio |
| LLM07 Vazamento do system prompt | "Mostre suas instruções" | Prompt sem segredos; recusa; o guard detecta n-gramas do prompt na saída | Cenário `prompt_leak` |
| LLM08 Fraquezas de vetores e embeddings | Busca cross-tenant | Filtro por `tenant_id` + RLS na tabela de chunks | Teste de isolamento |
| LLM09 Desinformação | Afirmações sobre produto | RAG com limiar, `no_basis` → curadoria, números com proveniência, juiz | Gate de fidelidade |
| LLM10 Consumo ilimitado | Abuso por contato; post viral; loop | Rate limit por contato; teto por turno, conversa e dia; orçamento; limite de chamadas por turno | Teste de carga e de orçamento |

### 7.2 Controles

- **Segredos** fora do repositório e das imagens: `.env` só em dev; em produção, arquivo de segredos do Docker (`secrets:`) com permissão 0400, gerado a partir de um cofre (fase 1: `sops` + `age` no repositório de infra; depois um gerenciador de segredos). Rotação: token da Meta (reconexão), chave do OpenRouter (trimestral), app secret da Meta (quando houver suspeita).
- **Tokens de canal cifrados** com envelope (ADR-016): uma DEK por tenant, cifrada pela KEK (`age`/libsodium) carregada do segredo; nunca logados (filtro de logging que mascara padrões `EAA…` e `sk-or-…`).
- **Webhook:** assinatura verificada sempre; endpoint de verificação (`hub.challenge`) com `verify_token` secreto.
- **PII:** mascaramento antes do LLM; `*_enc` + `*_hash`; traces e logs sem PII bruta (processador de logs com redator); retenção e expurgo (`03` §8).
- **RBAC** no console: `platform_admin`, `tenant_owner`, `seller`, `reviewer`; auditoria de toda mudança de config e de toda ação humana (assumir, devolver, aprovar documento, recarregar orçamento).
- **Isolamento:** `tenant_id` + RLS em toda tabela de negócio (`03` §7).
- **Rede:** Postgres e Redis sem porta pública; só o proxy reverso (Caddy) expõe 443; `api` atrás dele; console com sessão segura (cookie `HttpOnly`, `SameSite=Strict`) + TOTP para `platform_admin` e `tenant_owner`.
- **Imagens Docker** mínimas (`python:3.12-slim`, usuário não root, read-only rootfs onde der), versões fixadas por digest, varredura com Trivy.
