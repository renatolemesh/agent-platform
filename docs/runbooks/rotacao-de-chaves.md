# Rotação de chaves e segredos

Os segredos ficam em `docker/secrets/<nome>` (arquivos 0444 num diretório 0700) e entram nos containers como Docker secrets. O `scripts/docker/secrets-entrypoint.sh` os transforma nas variáveis que a aplicação lê. Depois de trocar um arquivo: `$C up -d --force-recreate <serviços>`. Toda rotação vai para o `audit_log` (o `rewrap_deks.py` grava sozinho; as outras, registre à mão).

**Gatilhos:** rotina (KEK anual, senhas semestrais), saída de alguém com acesso, suspeita de vazamento (rotação **imediata** de tudo o que a pessoa ou o sistema comprometido via) e o fim do uso de uma chave de teste.

## KEK (chave mestra do envelope, ADR-016)

A KEK cifra um DEK por tenant (`tenant_deks.wrapped_dek`); os campos cifrados usam o DEK e **não mudam** numa rotação de KEK.

```bash
# 1. gere a nova KEK (fora do servidor, no cofre)
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
# 2. ensaio (não grava nada; falha se a KEK antiga estiver errada)
$C run --rm --no-deps -e OLD_KEK_FILE=/run/secrets/kek_b64 -e NEW_KEK_B64='<nova>' \
   -v "$PWD/../scripts/ops:/ops:ro" worker-bg python /ops/rewrap_deks.py
# 3. aplicar (uma transação)
$C run --rm --no-deps -e OLD_KEK_FILE=/run/secrets/kek_b64 -e NEW_KEK_B64='<nova>' \
   -v "$PWD/../scripts/ops:/ops:ro" worker-bg python /ops/rewrap_deks.py --apply
# 4. trocar o segredo e recriar a aplicação (os processos guardam DEKs só em memória)
printf '%s' '<nova>' > secrets/kek_b64.new && chmod 0444 secrets/kek_b64.new && mv secrets/kek_b64.new secrets/kek_b64
$C up -d --force-recreate api worker worker-bg
```

Mantenha a KEK antiga no cofre até o próximo backup completo: backups anteriores à rotação têm `tenant_deks` cifrado com ela.

**Verificação:** abrir uma conversa com mensagens no console (decifra) e mandar uma mensagem de teste (cifra).

## DEK de um tenant

Trocar o DEK exige recifrar os campos do tenant (`messages.text_enc`, `channel_accounts.token_enc`, campos de formulários etc.). Não há ferramenta automática ainda; só faça em caso de comprometimento do DEK, com uma migração de dados revisada. No caso comum (suspeita sobre a KEK), rotacione a KEK.

## HASH_PEPPER

O pepper gera as colunas `*_hash` de busca (contatos, supressão). Trocá-lo invalida todas as buscas e a lista de supressão. **Não rotacione por rotina.** Em comprometimento: janela de manutenção com recálculo de todos os `*_hash` a partir dos valores decifrados, numa migração revisada.

## App secret da Meta (`meta_app_secret`)

1. Gere o novo no painel do app (Configurações → Básico → Redefinir).
2. Atualize `secrets/meta_app_secret` e recrie `api worker worker-bg` **imediatamente**: webhooks assinados com o segredo novo serão recusados (401) até lá, e a Meta reenvia por algum tempo.
3. Verifique: um webhook de teste chega (`inbound_events` novos).

## Token de verificação, sessão do console, webhook do Connect, `metrics_token`

Troque o arquivo e recrie os serviços. `console_session_secret` desloga todo mundo. `meta_verify_token` só é usado na (re)inscrição do webhook no painel: atualize lá também. `connect_webhook_secret` precisa ser trocado no Connect ao mesmo tempo.

## Senhas do Postgres e do Redis

```bash
NEW=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
$C exec postgres psql -U agentplatform -d agentplatform -c "alter role app_rw password '$NEW'"
printf '%s' "$NEW" > secrets/app_rw_password                  # 0444
$C up -d --force-recreate api worker worker-bg
```

Para o dono (`pg_owner_password`): `alter role agentplatform password ...` e atualize o arquivo; recrie `postgres backup migrate api worker worker-bg`. Redis: troque o arquivo e recrie `redis api worker worker-bg` (sem dados a perder).

## Chave do provedor de LLM (`openrouter_api_key`)

Crie a nova no painel com o mesmo limite de gasto, troque o arquivo, recrie a aplicação e revogue a antiga. A chave OpenAI usada em desenvolvimento (`.env` local) deve ser **revogada** no painel da OpenAI quando o desenvolvimento com ela terminar.

## Chave do wal-g (`walg_libsodium_key`) e credenciais S3

Credenciais S3: crie as novas no provedor, troque `walg_s3_access_key`/`walg_s3_secret_key`, recrie `postgres backup`, confira `pg_stat_archiver` e revogue as antigas.

A chave libsodium cifra os backups. Trocá-la exige um **base backup novo logo em seguida** (`$C exec backup walg-backup-loop --now`), e a chave antiga fica no cofre enquanto houver backups cifrados com ela dentro da retenção (14 dias).
