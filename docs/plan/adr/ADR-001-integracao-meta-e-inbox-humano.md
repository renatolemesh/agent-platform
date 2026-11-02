# ADR-001 — Integração direta com a Meta e porta do inbox humano

- **Status:** aceito (09/10/2026) · **Prioridades afetadas:** 2, 3

## Contexto
O MVP atende comentários e direct do Instagram de um consultor. O app Meta usa **Facebook Login** (conta IG ligada a uma página). O vendedor atende pelo app do Instagram. A integração com um inbox externo (Connect) fica fora do MVP. A doc oficial (consultada em 09/10/2026) diz que private replies e webhooks de comentário exigem **Advanced Access** (e o recurso Human Agent para private replies), o que implica verificação da empresa e App Review.

## Opções
1. **Graph API direto** (`graph.facebook.com`, Page token) + portas `ChannelAdapter`/`HumanInboxPort`.
2. Connect/Chatwoot como gateway do Instagram desde o MVP.
3. Ferramenta de terceiros (ManyChat etc.) para comentários.
4. Conversation Routing da Meta para passar o controle ao inbox da Meta.

## Decisão
Opção 1. O agente e o outbox dependem só das portas. A opção 4 fica como **sinal adicional opcional** (fase 5), porque a doc não diz como as respostas pelo app nativo interagem com o roteamento; o eco continua obrigatório. O Connect entra como adaptador de canal e de inbox na fase 5, só por API e webhooks, nunca pelo banco dele.

## Consequências
- (+) Controle total de janela, dedup, rate limit e eco; sem custo de terceiros.
- (−) **App Review e verificação da empresa ficam no caminho crítico** (risco R-01): começam no dia 1 da fase 1. O DM vai para produção antes dos comentários se o review atrasar.
- (−) O adaptador precisa acompanhar as versões da Graph API (`meta.graph_api_version`, hoje v26.0).
