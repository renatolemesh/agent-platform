# 09 — Follow-ups, importação de leads e campanhas

Follow-ups e campanhas são **máquinas de estado agendadas, não decisões do LLM**. O motor decide quando e para quem, respeitando janela, horário, consentimento e limites. O LLM só personaliza texto onde o canal permite, sempre passando pelo `output_guard`.

## 1. O que entra no MVP (Instagram)

O Instagram não deixa iniciar conversa com quem nunca escreveu, e envio pela API só vale até 24 h depois da última mensagem do usuário. Logo, no MVP:

- Follow-up **só dentro de 24 h da última mensagem do cliente** e **só das 8h às 22h** (America/Sao_Paulo); na prática, um ou dois lembretes curtos.
- **Nada de importação de base nem campanha ativa.** As tabelas e a máquina de estados já nascem genéricas, prontas para o WhatsApp (fase 4).
- `HUMAN_AGENT` nunca é usado por automação (`11` M3).

## 2. Modelo de cadência (config)

Mesmo formato do schema (`anexos/config_schema.py`, `FollowupsConfig` e `SendingConfig`):

```yaml
followups:
  cadences:
    - id: form_not_filled
      trigger: { event: stage_entered, stage: pre_cadastro_enviado }
      steps:
        - { delay: 4h, content_kind: template, template_key: form_reminder }     # fixed text approved by the client
      stop_on: [inbound_message, opt_out, handoff, ownership_human, stage_changed, form_submitted]
      max_attempts: 1
    - id: no_reply_after_answer
      trigger: { event: bot_message_unanswered, after: 3h, stages: [qualificando, educando, pronto_para_proposta] }
      steps:
        - { delay: 0m, content_kind: generated, prompt_version: followup_nudge@v1, max_chars: 200 }   # LLM within guardrails
      stop_on: [inbound_message, opt_out, handoff, ownership_human]
      max_attempts: 1
sending:
  quiet_hours: { start: "22:00", end: "08:00", timezone: America/Sao_Paulo }
  holidays_national: true
  holidays_local: []                     # feriados da cidade: "MM-DD" todo ano, "YYYY-MM-DD" uma vez
  proactive_jitter_s: [30, 300]
  frequency_cap_per_contact_day: 2       # sum of all flows
  frequency_cap_per_contact_week: 4
```

**Estados de `followup_runs`:** `scheduled → queued → sent | skipped(reason) | stopped(reason)`.

### Regras do motor

1. Gatilho cria `followup_runs` (único por `conversation, cadence, step`) no mesmo `commit` do turno.
2. O scheduler (`procrastinate` periodic, ADR-004) varre os `scheduled` vencidos a cada minuto e calcula o próximo instante válido: dentro da janela do canal **e** fora do horário silencioso **e** não feriado **e** abaixo do teto de frequência. Se o instante válido cair depois do fim da janela, o passo é `skipped(window_closed)`; no Instagram, ele **não** é reagendado.
3. Gera o conteúdo (template ou LLM com guard) e grava no `outbox` com `is_proactive = true`, `priority = 2` e `max_staleness` curto (10 min).
4. O despachante revalida tudo na saída (`07` §despachante). Qualquer "não" vira `skipped`, com motivo.
5. Condições de parada são avaliadas por evento (inbound, opt-out, handoff, posse humana, conversão) e cancelam passos futuros na mesma transação do evento. **Inbound do cliente para as cadências já no `ingress`**, antes do turno. Além disso, o despachante cancela qualquer item proativo se existir inbound com `provider_ts` posterior à criação do item (`07` §5, checagem 7), o que fecha a corrida "cliente escreve 1 s antes do follow-up sair" (revisão #10).

### Consciência de janela por canal

| Canal | Dentro da janela | Fora da janela |
| --- | --- | --- |
| Instagram, Messenger | Mensagem livre (template ou gerada) | Nada |
| WhatsApp (fase 4) | Mensagem livre | Só template aprovado com a categoria certa (`utility` ou `marketing`) e consentimento de marketing |
| Webchat (fase 5) | Se a sessão estiver ativa | E-mail, se houver consentimento |

## 3. Importação de leads (fase 4, modelo pronto agora)

- CSV/XLSX → mapeamento de colunas → **dry-run** com relatório (importados, atualizados, rejeitados com motivo) → confirmação → gravação em lote com `import_batch_id` → rollback do lote inteiro.
- Normalização: telefone em E.164 (`+55`, DDD, nono dígito, com `phonenumbers`), e-mail em minúsculas e validado, nome com capitalização.
- Deduplicação e merge pelas regras do `05` §identidade: merge automático só com identificador forte; o resto vira `contact_merge_suggestions`.
- **LGPD obrigatória por linha:** `source`, `legal_basis`, `purpose` e, quando a base for consentimento, `consent_evidence` (data, canal, texto aceito). Sem base válida, o contato entra marcado `not_contactable` e não aparece em segmento de campanha.
- **Supressão global:** quem está em `suppression_list` nunca volta por importação (checagem por hash antes de gravar).

Tabelas (fase 4): `import_batches(id, tenant_id, file_sha256, mapping, status, counts, created_by)`, `import_rows(batch_id, row_no, status, reason, contact_id)`.

## 4. Campanhas (fase 4)

- Definição: segmento (filtro sobre leads, tags, estágio, consentimento), template (WhatsApp: aprovado, categoria certa), agenda, limites, A/B.
- Throttling por portfólio, número e conta, respeitando o limite de mensagens iniciadas pela empresa do WhatsApp (por portfólio). Fila `priority = 3`; nunca atrasa conversa ativa.
- **Pausa automática** se a taxa de bloqueio ou de opt-out passar do limite (`campaign.auto_pause.block_rate > 2%` ou `optout_rate > 3%` em janela de 1 h) ou se a qualidade do número cair (webhook de qualidade).
- Resposta a campanha vira conversa normal, carregando `campaign_id` no `lead_sources` e no contexto.
- Métricas: enviados, entregues, lidos, respondidos, opt-outs, conversões e custo real lido do objeto `pricing` de cada status.
- Fluxos declarativos (YAML/JSON) validados por schema e editáveis no console; o construtor visual fica para a fase 5.

Tabelas (fase 4): `campaigns`, `campaign_variants`, `campaign_targets(campaign_id, contact_id, variant, status, outbox_id)`.

## 5. Testes

| Teste | Esperado |
| --- | --- |
| Follow-up agendado quando a janela de 24 h já fechou | `skipped(window_closed)`, nada enviado (diagrama em `07`) |
| Follow-up que cai às 22h05 | Reagendado para 8h + jitter **só se** ainda estiver dentro da janela; senão, `skipped` |
| Opt-out no meio da cadência | Passos futuros `stopped(opt_out)` na mesma transação; item já no outbox é cancelado pelo despachante (diagrama em `07`) |
| Vendedor assume a conversa | Todos os follow-ups `stopped(ownership_human)` |
| Teto de frequência | O 3º envio proativo do dia vira `skipped(frequency_cap)` |
| Feriado nacional | Nenhum proativo; respostas reativas seguem |
| Importação com contato suprimido | Linha rejeitada com motivo `suppressed` |
