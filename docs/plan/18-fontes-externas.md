# 18 — Fontes externas verificadas

Todas as fontes abaixo foram **consultadas em 09/10/2026** por três pesquisas paralelas. Marcação: **[O]** oficial, **[S]** secundária. Onde a doc divergiu do prompt, a doc vence e a divergência aparece na última coluna.

## 1. Meta — Instagram API with Facebook Login

| Fato | Fonte | Diverge do prompt? |
| --- | --- | --- |
| Graph API mais recente: v26.0 (29/07/2026); v25.0 expira em 29/07/2028 | [O] https://developers.facebook.com/docs/graph-api/changelog | — |
| Facebook Login → host `graph.facebook.com`; Instagram Login → `graph.instagram.com` | [O] https://developers.facebook.com/docs/instagram-platform/overview/ | — |
| Envio de DM: `POST /{PAGE_ID}/messages` (ou `/me/messages`), `recipient.id` = IGSID | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message | — |
| Private reply: `recipient.comment_id`; 1 por comentário; 7 dias; live só durante a transmissão; Requests se não segue; **exige `instagram_manage_comments`, `pages_messaging`, recurso Human Agent e Advanced Access** (a exigência do Human Agent é confirmada no app real no spike F1-META-05) | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/private-replies | **Sim**: Advanced Access necessário |
| Resposta pública: `POST /{ig-comment-id}/replies`; só 1º nível; não em ocultos nem em live | [O] https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-comment/replies | — |
| Ocultar comentário: `POST /{ig-comment-id}?hide=true` | [O] https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-comment | — |
| Conversations API: só as 20 mensagens mais recentes por conversa | [O] https://developers.facebook.com/docs/messenger-platform/conversations/ | — |
| Permissões de messaging: `instagram_basic`, `instagram_manage_messages`, `pages_manage_metadata`, `pages_show_list`, `business_management` | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/overview | — |
| Webhook `comments` exige Advanced Access e app em modo Live | [O] https://developers.facebook.com/docs/instagram-platform/webhooks | **Sim** |
| Standard Access: usuários com papel no app; Advanced exige empresa verificada (desde 01/02/2023) | [O] https://developers.facebook.com/docs/graph-api/overview/access-levels | Parcial |
| User token longo ≈ 60 dias; **Page token derivado não expira**, só é invalidado por eventos | [O] https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived | **Sim** (prompt: 60 dias, renovar) |
| `debug_token` retorna `is_valid`, `expires_at`, `data_access_expires_at`, `scopes` | [O] https://developers.facebook.com/docs/facebook-login/guides/access-tokens/debugging | — |
| Erro 190 com subcódigos 458/459/460/463/464/467/492; 4/17 throttling; 368 bloqueio | [O] https://developers.facebook.com/docs/graph-api/guides/error-handling | — |
| Acesso a dados expira em 90 dias sem atividade; permissões de página isentas | [O] https://developers.facebook.com/docs/facebook-login/auth-vs-data | — |
| Campos de webhook do Instagram (inclui `message_edit` desde 10/09/2025, `messaging_handover`, `standby`) | [O] https://developers.facebook.com/docs/instagram-platform/webhooks ; https://developers.facebook.com/docs/messenger-platform/changelog/ | — |
| Payloads: `is_echo`, `is_self`, `reply_to.story`, `story_mention`, `referral{ad_id, ref, source, type, ads_context_data}`, `postback`, `quick_reply` | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks ; https://developers.facebook.com/docs/instagram-platform/webhooks/examples | — |
| Eco do Messenger traz `app_id`; exemplos de eco do Instagram não trazem | [O] https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/message-echoes | Parcial |
| Webhooks: `X-Hub-Signature-256`; responder em até 5 s; retentativas por 36 h (Instagram) / desinscrição após 1 h de falha (Messenger Platform); sem ordem garantida | [O] https://developers.facebook.com/docs/instagram-platform/webhooks ; https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks | Acrescenta desinscrição |
| Limites: private replies 750/h (posts/reels), 100/s live; Conversations 2/s; Send 100/s texto, **10/s áudio e vídeo**; erro 80002 | [O] https://developers.facebook.com/docs/graph-api/overview/rate-limiting | Acrescenta 10/s |
| Janela de 24 h; `HUMAN_AGENT` 7 dias, só humano, com App Review | [O] https://developers.facebook.com/docs/features-reference/human-agent ; https://developers.facebook.com/documentation/business-messaging/messenger-platform/policy | — |
| Texto ≤ 1.000 bytes UTF-8 (a página também diz "< 1000 characters"); imagem 8 MB; áudio, vídeo e PDF 25 MB (PDF desde 19/12/2025) | [O] send-message (acima) | — |
| Quick replies ≤ 13 (≤ 20 chars, só mobile); generic template ≤ 10 elementos e 3 botões; ice breakers ≤ 4; `typing_on` desde 23/09/2025 | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/sender-actions e páginas de quick replies, generic template e ice breakers | — |
| Nenhuma chave de idempotência no envio | [O] (ausência na doc de send) | — |
| Handover Protocol substituído por Conversation Routing (23/10/2025); exige conta ligada a página | [O] https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/conversation-routing | — |
| URL de anexo em CDN "privacy-aware", sem prazo documentado | [O] instagram-platform overview | Lacuna |

### Publicação de posts (consultado em 03/11/2026, ADR-024)

| Fato | Fonte |
| --- | --- |
| Foto, vídeo, reels, carrossel e story: `POST /{IG_ID}/media` cria o container e `POST /{IG_ID}/media_publish` com `creation_id` publica. Permissões: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` | [O] https://developers.facebook.com/docs/instagram-platform/content-publishing/ |
| `GET /{container}?fields=status_code`: `EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, `PUBLISHED`. O container expira em 24 horas. Consultar 1 vez por minuto, por até 5 minutos | [O] mesma página |
| O limite diário aparece como 100 e como 50 posts, na mesma documentação. Dá para consultar em `content_publishing_limit` | [O] mesma página |
| O arquivo precisa estar em URL pública na hora da criação. Imagem só em JPEG | [O] mesma página |
| Legenda: até 2.200 caracteres, 30 hashtags, 20 menções. Imagem: até 8 MB, proporção de 4:5 a 1,91:1, largura a partir de 320 px. Reels: até 300 MB. Vídeo em story: até 100 MB | [O] https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/ |
| Atualizar um post só muda `comment_enabled` (com `instagram_manage_comments`). Legenda e arquivo não mudam | [O] https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/ |
| Apagar só com Facebook Login, com `instagram_manage_contents`. O título e o exemplo dizem `DELETE`, a sintaxe diz `POST`, e uma cópia antiga diz que não existe: **testar no primeiro uso** | [O] mesma página |

## 2. WhatsApp Cloud API (notificador e fase 4)

| Fato | Fonte | Diverge? |
| --- | --- | --- |
| Cobrança por mensagem desde 01/07/2025 | [O] https://developers.facebook.com/docs/whatsapp/pricing/ | — |
| **Desde 01/10/2026, mensagens de serviço e utility dentro da janela são cobradas** na tarifa de utility; Free Entry Point (72 h) segue grátis | [O] https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages | Confirma |
| Franquia de 1.000 mensagens de serviço grátis/mês por número | [S] https://whautomate.com/whatsapp-business-api-pricing-brazil (não confirmada na doc oficial) | Não confirmada |
| Tarifa Brasil: utility ≈ R$ 0,035; marketing ≈ R$ 0,32 | [S] mesma fonte; **confirmar no rate card oficial** | — |
| Cobrança em BRL disponível desde 01/07/2026 | [O] https://developers.facebook.com/docs/whatsapp/pricing/updates-to-pricing | — |
| Limites por portfólio: 250 / 2.000 / 10.000 / 100.000 / ilimitado (usuários únicos fora da janela em 24 h) | [O] https://developers.facebook.com/docs/whatsapp/messaging-limits | — |
| Templates: 250 sem verificação; revisão em até 24 h | [O] https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview | — |
| Webhook de botão: `type: "button"`, `button{text, payload}`, `context.id` | [O] https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/button | — |
| Termos (cláusula 4.7, rev. 23/09/2026): proíbem provedores de IA cuja IA é a funcionalidade principal | [O] https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform | A data de 15/01/2026 só aparece em fontes secundárias |
| Coexistência: `smb_message_echoes`, 20 msg/s, app ≥ 2.24.17 | [O] https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/ | — |
| Opt-in: claro, nomeando a empresa, com forma de sair | [O] https://developers.facebook.com/docs/whatsapp/overview/getting-opt-in | — |

## 3. LangGraph

| Fato | Fonte |
| --- | --- |
| `langgraph` 1.2.14 (06/10/2026), Python ≥ 3.10, MIT | [O] https://pypi.org/project/langgraph/ |
| `langgraph-checkpoint-postgres` 3.1.2; conexão com `autocommit=True` e `row_factory=dict_row`; `setup()`; `LANGGRAPH_STRICT_MSGPACK` | [O] https://pypi.org/project/langgraph-checkpoint-postgres/ ; https://docs.langchain.com/oss/python/langgraph/checkpointers |
| `durability = sync | async (padrão) | exit`, passado por execução (`invoke`/`stream`) | [O] https://reference.langchain.com/python/langgraph/types/Durability |
| `interrupt()` reexecuta o nó inteiro no resume | [O] https://docs.langchain.com/oss/python/langgraph/interrupts |
| `RunControl.request_drain` (cooperativo, entre super-steps); resume com `invoke(None, thread_id)` — **contestado na revisão independente; confirmar no spike F1-LG-01** (o plano não depende dele) | [O] https://docs.langchain.com/oss/python/langgraph/fault-tolerance |
| Double texting é do LangSmith Deployment, não do OSS | [O] https://docs.langchain.com/langsmith/double-texting |
| `ChatOpenRouter.with_structured_output` usa `function_calling` por padrão; `json_schema` com `strict` disponível | [O] https://reference.langchain.com/python/langchain-openrouter/chat_models/ChatOpenRouter/with_structured_output |

## 4. OpenRouter

| Fato | Fonte |
| --- | --- |
| `provider`: `order`, `allow_fallbacks`, `require_parameters`, `data_collection`, `zdr`, `only`, `ignore`, `sort`, `max_price` | [O] https://openrouter.ai/docs/guides/routing/provider-selection |
| Fallback de modelos com `models: [...]` | [O] https://openrouter.ai/docs/guides/routing/model-fallbacks |
| Structured outputs (`json_schema`, `strict`); parâmetros não suportados são ignorados em silêncio → `require_parameters: true` | [O] https://openrouter.ai/docs/guides/features/structured-outputs ; https://openrouter.ai/docs/api/reference/overview |
| ZDR: `zdr: true`; lista em `/api/v1/endpoints/zdr` | [O] https://openrouter.ai/docs/guides/features/zdr |
| Embeddings: `POST /api/v1/embeddings`; `qwen3-embedding-8b` US$ 0,01/M; `bge-m3` 0,01; `text-embedding-3-small` 0,02 | [O] https://openrouter.ai/docs/api/reference/embeddings ; https://openrouter.ai/api/v1/embeddings/models |
| Limite por chave (`limit`, `limit_reset`), guardrails com orçamento; `GET /api/v1/key` | [O] https://openrouter.ai/docs/guides/features/guardrails ; https://openrouter.ai/docs/api/reference/limits |
| `usage.cost` sempre presente (confirmar no spike F1-META-05; o gateway liquida pela estimativa se faltar); `GET /api/v1/generation?id=` | [O] https://openrouter.ai/docs/guides/administration/usage-accounting |
| Taxa de 5,5% na compra de créditos | [O] https://openrouter.ai/docs/faq |
| Prompt caching por provedor | [O] https://openrouter.ai/docs/guides/best-practices/prompt-caching |
| STT: `POST /api/v1/audio/transcriptions` (só `zdr`/`data_collection`) | [O] https://openrouter.ai/docs/guides/overview/multimodal/stt |
| Preços (in/out por 1M): gemini-2.5-flash-lite 0,10/0,40 (sem rota ZDR em 02/11/2026, fora da configuração); gemini-3.1-flash-lite 0,25/1,50; gpt-5-nano 0,05/0,40; gpt-5-mini 0,25/2,00; gpt-5.4-nano 0,20/1,25; gpt-5.4-mini 0,75/4,50; deepseek-v4-flash (DeepInfra) 0,09/0,18; mistral-small-2603 0,15/0,60; llama-4-maverick 0,19/0,65; claude-haiku-4.5 1,00/5,00; gpt-oss-120b 0,037/0,17 | [O] https://openrouter.ai/api/v1/models (preço por endpoint; conferir `usage.cost`) |

## 5. Dados, filas, observabilidade, avaliação, ingestão

| Fato | Fonte |
| --- | --- |
| pgvector 0.8.7 (01/10/2026); HNSW; `iterative_scan`; `halfvec` até 4.000 dims | [O] https://github.com/pgvector/pgvector |
| `pt_unaccent` (cópia de `portuguese` + `unaccent`) | [O] https://www.postgresql.org/docs/current/unaccent.html |
| RRF em SQL com k=60 | [O] https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py |
| Qdrant 1.19.2; memória ≈ n × dim × 4 B × 1,5 | [O] https://github.com/qdrant/qdrant/releases ; https://qdrant.tech/documentation/guides/capacity-planning/ |
| Voyage rerank-2.5 US$ 0,05/M (200M grátis); Jina reranker com pesos CC-BY-NC | [O] https://docs.voyageai.com/docs/pricing ; https://jina.ai/reranker/ |
| procrastinate 3.10.0 (23/09/2026): `lock`, `queueing_lock`, `RetryStrategy`, periódicos; job agendado com lock bloqueia os seguintes | [O] https://procrastinate.readthedocs.io/en/stable/howto/advanced/locks.html |
| Advisory locks de sessão incompatíveis com PgBouncer em modo transaction | [O] https://www.pgbouncer.org/features.html |
| Crítica ao Redlock (sem fencing token) | [S] https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html |
| Langfuse v4: self-hosted exige Postgres, ClickHouse, Redis e S3; compose recomenda 4 cores e 16 GiB; Hobby 50 mil unidades/mês, 30 dias | [O] https://langfuse.com/self-hosting ; https://langfuse.com/self-hosting/deployment/docker-compose ; https://langfuse.com/pricing |
| OTel Python 1.45.1; convenções GenAI em "Development" | [O] https://github.com/open-telemetry/semantic-conventions-genai |
| promptfoo 0.124.1, MIT, parte da OpenAI (anúncio de 09/03/2026) | [O] https://www.promptfoo.dev/blog/promptfoo-joining-openai/ |
| DeepEval 4.2.4: `ConversationSimulator`, `RoleAdherence`, `KnowledgeRetention`, `ConversationCompleteness` | [O] https://deepeval.com/docs/conversation-simulator |
| Ragas 0.4.3 (jan/2026); repositório `vibrantlabsai/ragas` | [O] https://github.com/vibrantlabsai/ragas/releases |
| DSPy 3.4.0; GEPA com adaptador LangChain/LangGraph | [O] https://dspy.ai/tutorials/gepa_ai_program/ ; https://github.com/gepa-ai/gepa |
| Contextual retrieval: −35% (embeddings), −49% (+BM25), −67% (+rerank) em falhas top-20 | [O] https://www.anthropic.com/news/contextual-retrieval |
| Docling 2.137 (MIT), memória alta em escaneados; OCRmyPDF 17.13 | [O] https://github.com/docling-project/docling ; https://ocrmypdf.readthedocs.io/ |
| Transcrição: Groq whisper-large-v3-turbo US$ 0,04/h; Deepgram Nova-3 US$ 0,0043/min; OpenAI gpt-4o-mini-transcribe ≈ US$ 0,003/min | [O] https://console.groq.com/docs/speech-to-text ; https://deepgram.com/pricing ; https://developers.openai.com/api/docs/pricing |

## 6. Regulação

| Fato | Fonte |
| --- | --- |
| Res. BCB 285/2023, art. 2º, XII: contrato traz condições de sorteio e regras do lance; arts. 12–13: lance e lance embutido | [O] https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Resolução%20BCB&numero=285 |
| Res. BCB 155/2021, art. 4º, III e IV: informação clara e precisa; art. 5º: taxa de administração, fundo de reserva e seguro em % do crédito | [O] https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Resolução%20BCB&numero=155 |
| Lei 11.795/2008, art. 22: contemplação por sorteio ou lance, conforme contrato | [O] https://www.planalto.gov.br/ccivil_03/_ato2007-2010/2008/lei/l11795.htm |
| Res. CD/ANPD 19/2024: cláusulas-padrão adotadas integralmente; prazo de adequação encerrado em 23/08/2025 | [O] https://www.gov.br/anpd/pt-br/acesso-a-informacao/institucional/atos-normativos/regulamentacoes_anpd/resolucao-cd-anpd-no-19-de-23-de-agosto-de-2024 |
| Guia de Agentes de Tratamento v2.0 (controlador, operador, suboperador) | [O] https://www.gov.br/anpd/pt-br/centrais-de-conteudo/materiais-educativos-e-publicacoes |
| Código de Conduta ABAC, art. 2º, IV e V: sem exagero, ambiguidade ou omissão; não explorar credulidade | [O] https://abac.org.br/nao-associados/a-abac/codigo-de-conduta/ |
| Decisões judiciais sobre promessa de contemplação rápida (citadas no prompt) | [S] https://legismap.com.br/conteudos/decisoes-dos-tribunais/consumidor-induzido-a-erro-em-contratacao-de-consorcio-tera-valores-restituidos-tjrs |

**Nota:** nem a Res. BCB 285 nem o código da ABAC proíbem **textualmente** "prometer contemplação". A vedação vem do dever de informação clara (Res. BCB 155, art. 4º), do CDC (publicidade enganosa) e da declaração do próprio contrato ("não recebi promessa de contemplação"). Para o agente, a regra é a mesma: proibido.
