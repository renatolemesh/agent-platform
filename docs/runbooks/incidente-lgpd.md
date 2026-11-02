# Incidente LGPD (vazamento) e pedido de titular

Papéis (plano `11`): o **tenant** (consultor) é o controlador; a **plataforma** é operadora. O encarregado e a assessoria jurídica entram em todo incidente. Prazos: a ANPD pede comunicação de incidente com risco relevante em prazo curto (hoje, 3 dias úteis pela Resolução CD/ANPD nº 15/2024). Confirme o prazo vigente com o jurídico no início do incidente.

## A) Suspeita de vazamento ou exposição de dados

### Sintomas

- Bot respondeu com dados de outro cliente; alerta de guard; reclamação; acesso indevido ao console; segredo exposto (repositório, log, print); backup ou dump fora do lugar.

### Ação imediata (primeiros 30 min)

1. **Conter:** kill switch do tenant (ou global) ([kill-switch.md](kill-switch.md)). Conversas afetadas para `PAUSED`.
2. **Preservar evidência:** não apague nada. Exporte, sem conteúdo de mensagem:
   ```bash
   P="$C exec postgres psql -U agentplatform -d agentplatform -c"
   $P "select at, actor, actor_kind, action, target from audit_log where at > now() - interval '7 days' order by at desc" > /tmp/incidente-audit.txt
   $P "select id, conversation_id, status, started_at from turns where tenant_id = '<tenant>' and started_at between '<de>' and '<até>'"
   ```
   Para o turno suspeito, use o **trace do turno** no console (contexto mascarado, trechos, guard, despacho).
3. **Segredo exposto:** rotacione imediatamente ([rotacao-de-chaves.md](rotacao-de-chaves.md)), revogue no provedor e procure uso indevido (painel do provedor, `llm_calls`, logs do Caddy).
4. **Acesso indevido ao console:** desative o usuário, reset de TOTP (console → Usuários), troque `console_session_secret` (derruba todas as sessões).

### Avaliação (até 24 h)

- Quais titulares, quais dados (identificação, contato, financeiros, **saúde** é dado sensível), quantos, por quanto tempo, se houve acesso por terceiros.
- Registre tudo num documento do incidente: linha do tempo, evidências, decisões.

### Comunicação

- A plataforma comunica o **controlador** (tenant) sem demora. O controlador decide, com o jurídico, a comunicação à ANPD e aos titulares (art. 48 da LGPD).
- Correção: cenário no lab reproduzindo o problema, correção, `lab gate --level full` passando, só então desligue o kill switch.

## B) Pedido de titular (acesso, correção, eliminação)

Canal: o titular pede ao consultor (controlador), que abre o pedido no console. Prazo de resposta: imediato no formato simplificado ou até 15 dias para a declaração completa (art. 19).

1. Console → **LGPD** → buscar o titular (telefone, e-mail ou id do Instagram). A busca usa `*_hash`; o texto não é exposto.
2. **Acesso/portabilidade:** **Exportar** gera um JSON com contato, identidades, conversas e mensagens decifradas, consentimentos e lead. Entregue ao controlador por canal seguro. A exportação fica auditada (`audit_log.action = 'lgpd.export'`).
3. **Correção:** pelo console da conversa/lead.
4. **Eliminação:** **Eliminar** com confirmação digitada. Em cascata: mensagens (`text_enc` e mídia nulos, `text_masked = '[eliminado]'`), resumo da conversa, payloads de `inbound_events`/`outbox`/`handoffs`, perfil do lead, campos do formulário e dados do contato apagados; identidades anonimizadas (`external_user_id = 'erased:<id>'`); identificadores do contato na `suppression_list` (motivo `erasure`) para não ser recontatado; `audit_log.action = 'lgpd.erase'`. O que a lei manda guardar (prova de consentimento, registros fiscais do controlador) fica conforme a política de retenção do tenant.
5. **Backups:** a eliminação não reescreve backups. Eles expiram pela retenção (14 dias) e um restore reaplicaria dados apagados: depois de qualquer restore, reaplique as eliminações registradas em `audit_log` desde a data do backup.

### Verificação

- Busca do titular retorna "eliminado"/vazio; contato na supressão; nenhuma campanha ou follow-up para ele.
- Linha em `audit_log` com quem pediu e quando.

## Retenção automática

O job diário `daily_maintenance` (03:30 UTC) roda `retention_sweep` por tenant com os prazos de `operations.retention_days` da config. Se o alerta de jobs falhos citar `daily_maintenance`, trate como pendência LGPD.
