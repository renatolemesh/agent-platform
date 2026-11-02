# ADR-016 — Segredos, tokens de canal e PII em repouso

- **Status:** aceito

## Contexto
Tokens da Meta (Page token sem expiração fixa, mas invalidável), chave do OpenRouter, token do WhatsApp e app secret não podem aparecer em log, imagem ou repositório. Campos pessoais precisam de cifragem em repouso.

## Opções
1. Variáveis de ambiente em texto puro.
2. **Docker secrets (arquivos 0400) decifrados no deploy a partir de `sops` + `age`**, e cifragem de envelope na aplicação para tokens e PII.
3. Vault/KMS gerenciado.

## Decisão
Opção 2 no MVP, com caminho para a 3:
- **KEK:** chave `age` carregada como secret.
- **DEK:** uma por tenant, cifrada pela KEK e guardada no banco.
- **Campos cifrados:** tokens e PII com AES-256-GCM (`cryptography`).
- **Busca:** `*_hash` com HMAC-SHA256 (pepper em secret).
- **Logs:** filtro de logging que mascara padrões de token.
- **Validação de token:** `token_guard` diário com `debug_token`.
- **Reação ao erro 190:** invalidação → kill switch + reconexão.

## Consequências
- (+) Vazamento do banco não expõe tokens nem PII sem a KEK.
- (−) Rotação da KEK exige recifrar as DEKs (script e runbook).
