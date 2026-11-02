# Token da Meta inválido / reconexão

## Sintomas

- Alerta `token_invalid:<tenant>:instagram:<conta>` ("Token do canal inválido").
- `channel_accounts.token_status = 'invalid'`; kill switch `channel/instagram` ligado com `reason='token_invalid'` (o `token_guard` diário faz isso sozinho, plano `07` §8.8).
- Envios cancelados com `cancel_reason` de kill switch; erros 190 da Graph API no log do worker.
- Causas comuns: o dono trocou a senha do Facebook, removeu o app, perdeu o papel na página ou o token expirou.

## Diagnóstico

```bash
$C exec postgres psql -U agentplatform -d agentplatform -c "
  select t.slug, ca.channel, ca.external_account_id, ca.token_status, ca.token_last_validated_at, ca.scopes
  from channel_accounts ca join tenants t on t.id = ca.tenant_id where ca.token_status <> 'valid'"
$C exec postgres psql -U agentplatform -d agentplatform -c "
  select tenant_id, scope, scope_ref, is_on, reason, changed_at from kill_switches where scope = 'channel' and is_on"
```

No console (como dono do tenant): **Canais** mostra o status, a última validação e os escopos.

## Ação

1. Avise o dono do tenant (WhatsApp/e-mail) com o link `https://<domínio>/console/channels`.
2. O dono clica em **Reconectar** e refaz o Facebook Login, concedendo **todos** os escopos pedidos e selecionando a página certa. O callback grava o token novo cifrado, roda `debug_token`, marca `token_status='valid'` e desliga o kill switch do canal.
3. Se o dono não puder agir: o canal fica pausado (seguro). Mensagens novas continuam sendo gravadas; nada é enviado.
4. Depois da reconexão, o outbox retido é revalidado pelo despachante: o que passou da janela de 24 h ou ficou velho (`max_staleness_s`) é **cancelado**, não enviado atrasado. Se havia conversas quentes, abra handoff para o vendedor retomar manualmente.
5. Confira a inscrição de webhooks (o `token_guard` diário também reinscreve): `GET /{page-id}/subscribed_apps` precisa listar `messages`, `messaging_postbacks`, `message_echoes`, `comments` etc.

## Verificação

- `token_status='valid'`, `token_last_validated_at` recente, kill switch do canal `is_on=false`.
- Mensagem de teste para a conta recebe resposta; `inbound_events` voltam a chegar (o alerta `no_inbound` some).
