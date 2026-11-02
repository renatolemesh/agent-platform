# ADR-002 — Detecção de humano por eco

- **Status:** aceito · **Prioridades afetadas:** 2

## Contexto
O vendedor responde pelo app do Instagram, e não há evento explícito de "humano assumiu". Mensagens enviadas pela conta chegam como eco (`is_echo`). Os exemplos oficiais de eco do Instagram não mostram `app_id` (o Messenger mostra). Não há chave de idempotência no envio, e o eco do nosso envio pode chegar antes da resposta HTTP com o `mid`.

## Opções
1. Eco cujo `mid` não está no outbox = humano, com janela de graça e registro da tentativa antes do envio.
2. Só o botão "assumi" (manual).
3. Conversation Routing (eventos de troca de dono).
4. `app_id` no eco (se existir).

## Decisão
Opção 1 como sinal principal. A opção 2 cobre GIF e sticker sem webhook e o esquecimento do vendedor. A 4 entra como reforço **se** o teste da fase 1 mostrar `app_id` no eco do Instagram. O despachante grava `attempt_registered_at` + `content_hash` antes de chamar a API. O eco que casa com um item `sending` **ou `unknown`** (envio ambíguo) dentro de `echo_reconcile_window_s` (120 s) é nosso e confirma o envio, então um eco atrasado nunca pausa o bot por engano. O eco que não casa espera `echo.grace` (3 s) e é reconferido antes de virar sinal de humano. Remetentes conhecidos (automações nativas) ficam numa allowlist. O job de eco não usa o lock da conversa.

## Consequências
- (+) Funciona com o app nativo, sem mudar o hábito do vendedor.
- (−) Até `echo.grace` de atraso para pausar o bot. Mitigado porque o despachante confere `send_epoch` e posse antes de cada bolha.
- Testes obrigatórios: "vendedor responde enquanto o LLM gera" e "eco antes do `mid`" (`07` §8.3 e §8.4).
