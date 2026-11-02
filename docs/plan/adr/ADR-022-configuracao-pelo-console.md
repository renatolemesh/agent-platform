# ADR-022 — Configuração pelo console

- **Status:** aceito (02/11/2026). Complementa o ADR-014; muda o F2-CON-04.

## Contexto
Só o texto dos prompts e o material tinham tela. Todo o resto era um YAML inteiro numa caixa de texto. Ativar em produção exigia rodar `lab gate` no terminal, e o segundo aprovador só podia ser criado por lá. Ao cruzar a configuração com o código, apareceram 44 opções sem efeito, entre elas `tone`, `waiting_message` e o limite que impede citar material interno. Havia também um defeito de fundo: cada rascunho era uma cópia da versão ativa, e aprovar um rascunho antigo desfazia, sem aviso, o que tinha sido ativado depois.

## Decisão
**Formulários no lugar de YAML, para o que se muda no dia a dia.** Assistente, Regras, Modelos, Testar a base, Usuários e Preparação. O YAML continua em Configuração, como opção avançada, para o resto. Os formulários não têm regra própria: enviam campos para `PlatformConfig`, que valida como antes, e as respostas seguem passando pelos guards.

**Instruções do negócio em vez de editar o prompt.** `conversation.business_instructions` e `conversation.reply_examples` entram no `respond@v9` abaixo das regras inegociáveis, que não ficam ao alcance do dono da conta. O editor de prompt continua, como avançado. O texto diz ao modelo que instrução nunca é fonte de valor, prazo ou promessa, e os guards conferem isso de qualquer forma.

**Todo rascunho sabe de onde veio.** `config_versions.base_version_id`:
- Aprovar um rascunho feito de outra versão é recusado (`stale_draft`).
- **Atualizar rascunho** reaplica a mesma mudança sobre a versão atual. Se os dois lados mudaram o mesmo campo, para e pede uma pessoa. A nova versão é do mesmo autor, e precisa de nova avaliação.
- Mudança é sempre construída sobre o que está no banco (`Runtime.tenant_for_change`), nunca sobre a cópia em memória. O teste real achou esse caso: um formulário aberto logo depois de uma aprovação carregava os valores antigos, e salvar desfazia a aprovação.

**Regra de duas pessoas mantida.** Decisão do dono: quem propõe não aprova, e o dono da plataforma é o segundo aprovador de um vendedor que trabalha sozinho. O custo é que cada ajuste depende de outra pessoa.

**Avaliação pelo console.** Um botão pede a avaliação, e o worker roda (`lab.gate`, fila `lab`). Só a completa libera a aprovação em produção, e só para aquele rascunho. Roda sempre no canal `simulator`: nenhum cliente recebe nada, nenhum vendedor é avisado, fica fora da caixa e dos painéis, e pode rodar antes do `go_live` e com os workers no ar. Antes, o gate usava a conta real do Instagram, e os workers podiam tentar enviar de verdade. Para no teto `budget.experiment_cap_usd`, e o que não terminou não passa. Os limites do gate são regra da plataforma e não ficam na configuração da conta: ninguém baixa a régua da própria mudança.

**Toda opção faz alguma coisa.** `tests/contract/test_config_is_read.py` falha se um campo não tem quem o leia. Campo de um valor só (`Literal[True]`) é uma invariante declarada, não uma opção. Opção removida segue carregando em versões antigas (`Strict._removed`), ou um rollback quebraria.

| Passou a valer | O que faz |
| --- | --- |
| `output_guard.guidance_only_max_ngram` | Bloqueia 12 palavras seguidas de trecho "Só orienta" (regra `guidance_quoted`) |
| `agent.turn_timeout_s` | Turno lento termina como queda de modelo: mensagem curta e vendedor |
| `agent.slow_after_s`, `conversation.waiting_message` | Uma mensagem de espera, pelo outbox, com todas as checagens |
| `conversation.bubble_delay_ms` | Intervalo entre bolhas, com as checagens de novo antes de cada uma |
| `input_guard.per_contact_messages_per_min` | Acima disso não roda turno e não gasta; as mensagens ficam pendentes; pedido para parar passa |
| `sending.frequency_cap_per_contact_week`, `proactive_jitter_s` | Teto semanal, somando os canais da pessoa, e follow-ups que não saem todos juntos |
| `persona.assistant_name`, `persona.tone` | Entram no `respond@v9` |
| `guidelines.overrides`, `funnel.max_turns_without_progress_override`, `tools` | Desligar diretriz, mudar paciência por estágio, desligar ferramenta |
| `handoff.closed_reopens_on_inbound` | Conversa encerrada pode continuar encerrada |
| `budget.experiment_cap_usd` | Teto de uma avaliação |

**Regras novas que a avaliação real mostrou que faltavam.** Rodando os casos com a IA de verdade, a resposta seguia estas regras só quando o modelo obedecia ao prompt. Agora o código garante:
- **Identificação na primeira mensagem:** se o modelo esquecer, o código acrescenta (`conversation.first_message_disclosure`).
- **Afirmar sem base:** com a busca vazia, a resposta não pode abrir com "sim", "pode", "não tem" (`output_guard.no_basis_claim_patterns`). Volta para reescrever; se insistir, vai para o vendedor. Na avaliação, o modelo de dev abriu com "Pode sim" sem nada que sustentasse.
- **Número com fonte:** `1546.67` numa ferramenta contava também como `1546` e `67`, e um `R$ 67` inventado passava. Agora é só aquele número.

**Removidas**, porque nunca fizeram nada: `rerank`, `pre_rerank_k`, `analyze_mode`, `day_boundary_tz`, `degradation_order`, `ci_daily_cap_usd`, `heartbeat_timeout_s`, `repetition_similarity_block`, `raw_media_retention_days`, `channel_rate_safety_margin`, `trace_sampling`, `operations.canary_percent`, `Cadence.stop_on`, `meta.*`, `notifier*`, `return_to_bot_after_inactivity_min`, as regras antigas de comentário. `triggers_enabled` virou `handoff.disabled_reasons`: desligar é só para o que o pacote deixa, e cancelamento, pós-venda, jurídico e pedido de falar com alguém sempre chamam o vendedor. Valores fixos no código viraram configuração: `rag.candidates_k`, `rag.max_chunks_per_turn`, `rag.degraded_faq_min_rank`, `agent.max_reply_attempts`, `output_guard.max_bytes`, `output_guard.repetition_window` e os textos de `guidelines.*`.

**Vetores pertencem a um índice.** Vetor de um modelo não significa nada para outro.
- `faq_variant_vectors` substitui `faq_variants.embedding`, como `kb_chunks` já fazia.
- Configuração com modelo diferente do índice é recusada, na proposta e na ativação (`embedding_mismatch`). Se chegar a uma busca mesmo assim, ela nunca compara: usa só FAQ por palavras ou diz que vai confirmar.
- **Trocar o modelo** constrói um índice novo ao lado do atual, só com o material em uso, em lotes que não se perdem numa falha. Clientes continuam no índice atual até uma pessoa aprovar o rascunho que o job cria, então canário e rollback seguem funcionando. A varredura cobre o que foi lido ou aprovado no intervalo.

**Avisos que não tiram nada de uso.** Trechos parecidos com números diferentes, em documentos em uso, viram `kb_conflicts`: os vetores já estão no banco e nada sai do servidor. Perguntas parecidas da curadoria são agrupadas por palavras (`question_similarity`), localmente, e quem responde uma responde o grupo. Pode separar se o grupo estiver errado.

**Entrar e sair do console.** Convite por link que funciona uma vez e expira: guardamos o hash, e a senha é criada por quem entra. Desativar ou mudar o papel encerra a sessão aberta (`session_alive`, a cada requisição, com cache de 10 s). Antes, o cookie seguia valendo por até 8 horas.

## Consequências
- (+) O dono muda quase tudo sem terminal, e quem aprova lê o que muda em palavras.
- (+) Uma opção que não faz nada não entra de novo: o teste de contrato barra.
- (+) O gate pode rodar em produção com os workers no ar.
- (−) Mais uma leitura ao banco por requisição do console, atenuada pelo cache.
- (−) A avaliação completa gasta do orçamento diário da conta, como o simulador.
- (−) Trocar o modelo de busca grava uma cópia dos trechos por índice até a próxima limpeza.
- (−) Agrupar por palavras erra em paráfrases sem palavras em comum.

## O que estas regras não pegam
Citação de material interno só é barrada a partir de 12 palavras seguidas. Com outras palavras, depende do prompt: nos testes, o modelo de dev repetiu um argumento de venda quando o conjunto de testes pediu o material. A resposta "confirmo depois" sem abrir com afirmação também não é verificada por código.

## O que ficou de fora
Criar conta nova pelo console (`bootstrap` segue no terminal, e Preparação mostra o que falta depois dele), rerank, editar o texto de um trecho, placar de experimentos e varredura de modelos na tela, otimização automática de prompt. O conjunto `consortium/rag-v1` com o material real depende do material aprovado e do OpenRouter; hoje a avaliação de busca roda sobre um corpus sintético.
