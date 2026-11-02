# ADR-013 — Formato do pacote de segmento

- **Status:** aceito

## Contexto
O núcleo não pode depender de consórcio. Um segundo pacote fictício precisa passar na suíte genérica. Ferramentas de domínio entram por interface de plugin.

## Opções
1. Só dados (YAML/JSON) no banco, com ferramentas genéricas configuráveis.
2. **Pacote Python** (entry point `agent_platform.segment_packages`) com manifesto YAML validado + ferramentas em código.
3. Microserviço por segmento.

## Decisão
Opção 2:
- O manifesto (glossário, slots, estágios, transições, fluxos, diretrizes, compliance, fontes, FAQs semente, cadências e fatos-chave) é validado por `PackageManifest`.
- As ferramentas de domínio são `ToolSpec` em código determinístico e testado.
- Os datasets estruturados ficam em `domain_datasets`, com schema declarado pelo pacote.
- O tenant fixa `name@version`.
- Overrides do tenant passam pela regra de monotonicidade de compliance.

## Consequências
- (+) Cálculos (simulação) ficam em código testável, nunca no LLM; o núcleo segue agnóstico.
- (−) Mudança de ferramenta de domínio exige release do pacote (não é só config), o que é aceitável.
- Prova: `tests/contract/` roda para `consortium` e `auto_insurance`, mais o grep de termos de segmento em `src/core`.
