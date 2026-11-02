# ADR-017 — `understand_turn` e `classify_stage` numa chamada (`analyze_turn`)

- **Status:** aceito (provisório até a varredura do laboratório na fase 2)

## Contexto
A arquitetura de referência roda `understand_turn` e `classify_stage` em paralelo, com duas chamadas de modelo pequeno. Com US$ 5 iniciais e p95 de latência alvo, cada chamada por turno pesa. O próprio prompt permite fundir se o laboratório decidir.

## Opções
1. Dois nós paralelos (2 chamadas).
2. **Um nó com saída estruturada contendo `TurnUnderstanding` e `StageProposal`** (1 chamada).

## Decisão
Opção 2 como padrão (`agent.analyze_mode: merged`), com a 1 disponível por config. O contrato de saída continua separado, e a aplicação do estágio continua sendo decisão do código (transição permitida + limiar + evidência).

## Consequências
- (+) Economiza ~30% do custo do modelo pequeno e ~1 s de latência por turno.
- (−) Possível perda de acurácia de estágio. Gate: acurácia de estágio e F1 de slots no `consortium/understand-v1`. Se `merged` ficar mais de 3 pontos abaixo de `parallel`, troca para `parallel`.
