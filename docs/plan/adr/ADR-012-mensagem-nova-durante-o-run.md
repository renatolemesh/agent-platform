# ADR-012 — Mensagem nova durante o run

- **Status:** aceito

## Contexto
Double texting é recurso do LangSmith Deployment, não da biblioteca open source (confirmado em 09/10/2026). A política fica por nossa conta.

## Opções
1. `enqueue`: a mensagem nova sempre vira o próximo turno.
2. `interrupt`/`rollback`: sempre cancela o run e reinicia com o lote combinado.
3. **Híbrida:** se nada foi gravado no outbox ainda, cancela e reinicia com o lote combinado; se já houve commit, o lote novo vira o próximo turno.

## Decisão
Opção 3 (`concurrency.new_message_policy = cancel_if_unsent_else_next_turn`):
- **"Nada enviado ainda" = turno ainda `running` (não comitado).** A decisão é tomada sob o mesmo advisory lock do commit, então é atômica com ele. A mensagem de espera (`turn_id:wait`, gravada pelo orquestrador) não conta como envio.
- **Cancelamento:** incrementa `run_epoch` + `cancel:{conv}`; o worker cancela a task asyncio do run (ou o run falha no fencing do commit). `RunControl.request_drain` só será usado se o spike F1-LG-01 confirmar a API.
- **Depois do commit:** o lote novo vira o próximo turno, e as bolhas restantes do turno anterior seguem (o `send_epoch` não muda).
- **Teto:** 2 reinícios seguidos.
- **Exceção:** opt-out ou pedido de humano no lote novo cancela também o outbox pendente.
- Toda decisão fica registrada em `turns.cancel_reason`.

## Consequências
- (+) A resposta considera tudo o que o cliente disse antes de qualquer envio, sem duplicar nem contradizer.
- (−) O custo de LLM dos runs cancelados é perdido (o debounce reduz a frequência).
