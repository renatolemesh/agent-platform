# ADR-014 — Registro de configuração

- **Status:** aceito

## Contexto
Tudo precisa ser configurável sem deploy, versionado, auditável, com canário e rollback, e cada turno precisa ser reproduzível.

## Opções
1. Arquivos YAML no Git + deploy.
2. **Documentos JSONB versionados no Postgres**, validados por Pydantic, editados pelo console/Admin API.
3. Serviço de feature flags externo.

## Decisão
Opção 2:
- `config_versions` com status (`draft`, `approved`, `canary`, `active`, `superseded`, `rolled_back`), `content_hash` e `eval_run_id`.
- Resolução: plataforma ⊕ pacote ⊕ tenant ⊕ variante do laboratório.
- Dupla aprovação para prompts e compliance.
- Canário por hash de `conversation_id`.
- Cache de 30 s, com `config_version_id` gravado em cada turno.
- Uma exportação da config ativa vai para o Git como backup legível (job diário).

## Consequências
- (+) Hot reload, auditoria e reprodutibilidade.
- (−) Edição concorrente exige controle otimista (`version`).
