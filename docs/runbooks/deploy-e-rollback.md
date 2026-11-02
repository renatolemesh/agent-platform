# Deploy e rollback

**Política:** imagem construída no CI e identificada por sha; migrações só para frente e compatíveis com a versão anterior (expand/contract), então rollback = voltar a imagem, **sem** desfazer migração.

## Primeiro deploy (servidor novo)

1. VPS Ubuntu 24.04, Docker Engine + Compose v2, usuário `deploy` sem root no grupo `docker`, UFW liberando só 22 (IPs do operador), 80 e 443.
2. Código em `/srv/agentplatform` (clone do repositório; só `docker/`, `scripts/` e `migrations/` são usados no host).
3. Segredos e configuração:
   ```bash
   scripts/deploy/init-secrets.sh                 # gera senhas, KEK, pepper, chave do wal-g; cria vazios os de terceiros
   $EDITOR docker/secrets/openrouter_api_key docker/secrets/meta_app_secret docker/secrets/walg_s3_access_key docker/secrets/walg_s3_secret_key
   cp docker/prod.env.example docker/prod.env && $EDITOR docker/prod.env
   printf 'PUBLIC_DOMAIN=agente.exemplo.com.br\nACME_EMAIL=ops@exemplo.com.br\n' > docker/.deploy.env
   mkdir -p docker/tenants docker/materiais      # o arquivo da conta e o material ficam só aqui, fora do Git; precisam ser legíveis pelo uid 10001
   ```
   Guarde **agora** `kek_b64`, `hash_pepper_b64` e `walg_libsodium_key` no cofre offline: sem eles nem o banco nem os backups podem ser decifrados.
4. DNS do domínio apontando para a VPS (o Caddy emite o certificado no primeiro acesso).
5. `scripts/deploy/deploy.sh ghcr.io/<org>/agentplatform:<sha>`.
6. Bootstrap do tenant (uma vez, com `docker/tenants/<slug>.yaml` no servidor; ver go-live): `$C run --rm --no-deps migrate agentplatform bootstrap --slug <slug> --page-id ... --seller-email ...`.

## Deploy de rotina

```bash
cd /srv/agentplatform && git pull                        # scripts/compose atualizados
scripts/deploy/deploy.sh ghcr.io/<org>/agentplatform:<sha>
```

O script faz: `pull` → `build postgres` (no-op se não mudou) → `run --rm migrate` → `up -d` → espera o healthcheck do `api` → testa `https://$PUBLIC_DOMAIN/health` → registra a imagem em `docker/.deployed`.

**Antes** de ativar mudança de prompt/modelo/config: `agentplatform lab gate --level full` precisa passar (CLAUDE.md). Config de tenant não precisa de deploy: vai pelo console (proposta → aprovação → canário).

## Sintomas de deploy ruim

- `deploy.sh` termina com "api is not healthy"; `$C ps` mostra `unhealthy`/reiniciando.
- Aumento de `turn_failures`, `outbox_stuck` ou erros 5xx logo depois do deploy.

## Diagnóstico

```bash
$C ps
$C logs --since 10m api worker | tail -200
$C exec postgres psql -U agentplatform -d agentplatform -c "select version_num from alembic_version"
$C exec postgres psql -U agentplatform -d agentplatform -c "select status, count(*) from turns where started_at > now() - interval '15 min' group by 1"
```

## Ação: rollback

```bash
scripts/deploy/deploy.sh --rollback        # volta para a imagem anterior registrada em docker/.deployed
# Se uma config foi publicada depois da imagem atual, faça rollback da config antes (console > Configuração):
# a imagem antiga recusa documento com campo que ela não conhece, e o turno falha com ValidationError.
```

Se o problema é comportamento do agente (e não crash), **primeiro** ligue o kill switch do tenant ([kill-switch.md](kill-switch.md)), depois faça o rollback.

Se uma migração quebrou a versão anterior (violação da política expand/contract): não tente `alembic downgrade`. Corrija com uma migração nova para frente; em último caso, restaure o backup ([restore-de-backup.md](restore-de-backup.md)).

## Verificação

- `$C ps`: todos `healthy` (o `backup` fica `starting` nos primeiros 10 min).
- `curl -s https://$PUBLIC_DOMAIN/health` → `{"ok":true,"spool_bytes":0}`.
- No console: conversa de teste do vendedor recebe resposta; `turns` recentes com `status='committed'`.
