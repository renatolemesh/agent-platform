# Status da implementação

Data: 2026-11-02 · referência: `docs/plan/14-roadmap.md` · testes: **745 passando** (`uv run pytest`), mais 2 testes
ao vivo fora do padrão (`-m live`) · lint (ruff) e mypy limpos.

Legenda: ✅ feito e testado · 🟡 feito em parte (o que falta está na linha) · ⛔ depende de algo externo · ⬜ não feito.

## Fase 1 — Fundação

| ID | Status | Onde / observação |
| --- | --- | --- |
| F1-REPO-01 | ✅ | uv, `CLAUDE.md`, ruff + mypy (`pyproject.toml`), `.pre-commit-config.yaml` (bloqueia `.env`, PDFs, `docs/materiais/`, chaves) e CI em `.github/workflows/ci.yml`. ⛔ O CI só foi validado como YAML; roda no primeiro push ao GitHub. |
| F1-INFRA-01 | ✅ | `docker/compose.yaml` (dev, a app conecta como `app_rw`) e `docker/compose.prod.yaml` (Caddy, rede interna, segredos em arquivo, sistema de arquivos somente leitura, limites para 2 vCPU / 4 GB). Smoke da stack de produção feito localmente. |
| F1-DB-01 | ✅ | `migrations/sql/0001–0005`. RLS com `app_rw` + `FORCE`. `test_rls_isolation.py` gera os casos a partir do catálogo. |
| F1-SEC-01 | ✅ | `core/security/` + `scripts/ops/rewrap_deks.py` (rotação da KEK, com dry-run). |
| F1-GW-01 | ✅ | `api/webhooks.py`: assinatura, spool, `inbound_events` idempotente. |
| F1-Q-01 | ✅ | `jobs/app.py`: filas, retry, periódicos (minuto e diário), retry de jobs travados. |
| F1-OUT-01 | ✅ | `core/outbox/dispatcher.py`: 11 checagens + kill switch por capacidade (agente, comentários, follow-ups, campanhas). |
| F1-OWN-01 | ✅ | Posse, epochs, lease, `privacy_gate`, detector de eco. |
| F1-DEB-01 | ✅ (desvio) | Debounce no Postgres (ver desvios). |
| F1-MG-01 | ✅ | Gateway + orçamento: tetos da chave e do tenant (dia, **total do piloto**, conversa, turno) em toda chamada atribuída ao tenant (turno, transcrição, resumo). |
| F1-CFG-01 | ✅ | Registro com versões, rollback, aprovação por duas pessoas e **canário por conversa**, que chega ao turno (`Runtime.runner`). |
| F1-PKG-01 | ✅ | Entry points + `tests/contract`. |
| F1-OBS-01 | ✅ | `GET /metrics` (Prometheus, calculado do banco), alertas ao operador a cada minuto (`core/ops/observability.py`, limiares em `operations.alerts`), tracing OpenTelemetry opcional (`core/ops/tracing.py`) e perfil `obs` em `docker/obs/` ([observabilidade.md](runbooks/observabilidade.md)). |
| F1-EVAL-01 | ✅ | Runner, gate rápido e completo; o gate rápido está no CI. |
| F1-META-01..05 | ⛔ | Exigem o app Meta real, verificação da empresa e App Review. |
| F1-META-03 | ✅ | `token_guard` liga o kill switch quando o token é inválido; a reconexão via Facebook Login (`core/channels/instagram/oauth.py`, tela Canais) troca o token e libera o canal. Testado com a Meta simulada. |
| F1-WA-01 | 🟡 | Notificador, botões assinados e links de e-mail (`/actions/...`, confirmados por POST) prontos. ⛔ Faltam o número e os templates `handoff_alert_v1` / `ops_alert_v1`. |
| F1-OPS-01 | ✅ | wal-g: WAL contínuo cifrado (perda máxima de 5 min) + base diária; `scripts/backup/restore_test.sh` e `selftest.sh` (cadeia completa e PITR testados localmente); 12 runbooks em `docs/runbooks/`. ⛔ Falta configurar o destino S3 real. |
| F1-ING-01 | ✅ | Ver "Material do cliente". |
| F1-RL-01 | ✅ | Token buckets (Redis/local) + limite por IP no formulário público. |
| F1-SPOOL-01 | ✅ | Spool durável com **replay automático a cada 30 s**. Cada lote só é apagado depois de gravado, então um crash no meio do replay não perde mensagens. ⛔ A reinscrição e o backfill dependem do app real. |
| F1-LG-01 / F1-Q-02 | ✅ | Spikes resolvidos. |

## Fase 2 — MVP Instagram

| ID | Status | Onde / observação |
| --- | --- | --- |
| F2-AG-01..06 | ✅ | Grafo, guards, política, prompts `@v2`; `compliance-v1` 100%. |
| F2-KB-01 | ✅ | FAQ + híbrido RRF + `no_basis`. |
| F2-PKG-01 | ✅ | Pacote `consortium`. |
| F2-MEDIA-01 | ✅ | `core/media/processor.py`: áudio pelo gateway, com teto de duração e orçamento do tenant (**transcrição real testada ao vivo**). Imagem com OCR local (tesseract na imagem Docker). PDF com extração local. Todo texto é mascarado. Download do WhatsApp autenticado. `refuse_and_handoff` encaminha ao humano. `llm_describe` cai no OCR (não implementado). |
| F2-CMT-01 | ✅ / ⛔ | Configurado por post no console (ADR-018). Testado com contas reais pela busca periódica; o webhook de comentários só chega depois do App Review. |
| F2-ENTRY-01 | ✅ | Story, postback e referral → `lead_sources`, e o agente recebe a origem (ADR-018). Perguntas prontas (ice breakers) publicadas pela tela Posts. |
| F2-FORM-01/02 | ✅ | `/f/{token}`: uso único mesmo com envios simultâneos, token CSRF, limite por IP, CSP sem scripts, `no-store`, campos proibidos descartados. |
| F2-FU-01 | ✅ | Follow-ups. |
| F2-CON-01..03 | ✅ | Login com TOTP (obrigatório por papel via `CONSOLE_TOTP_REQUIRED_ROLES`) e bloqueio após falhas; CSRF em todo POST; conversas, trace, curadoria e documentos. O administrador da plataforma escolhe o tenant em `/console/tenants`. E2E com navegador em `tests/e2e`. Material, perguntas frequentes e curadoria pelo console (ADR-021). |
| F2-CON-04 | ✅ | Tela Config (diff, proposta, aprovação por outra pessoa, canário, promover/abortar, rollback), Orçamento (gasto e recarga, que passa pela aprovação), kill switches por tenant e por capacidade. |
| F2-LAB-01 | 🟡 | Datasets, runner, teto de custo, `experiments`. **Simulador no console** (`/console/simulator`, `lab/simulator.py`): conversa com o agente real num canal isolado (`simulator`), com a config ativa ou um rascunho não aprovado, explicação do turno (estágio, intenções, diretrizes, trechos, revisão de segurança, custo) e reexecução da mesma conversa com outra versão. Não notifica vendedor, não aparece na caixa de entrada nem nos painéis (migração 0006). Falta execução paralela de datasets pela UI. |
| F2-LAB-04 | ✅ | **Painel de prompts** (`/console/prompts`): edita o prompt de cada etapa (resposta, entendimento, revisão, resumo), salva como versão imutável do tenant (`prompt_templates`, rótulo `<etapa>@t-xxxxxxxx`, RLS, migração 0007), valida seções/sintaxe/variáveis e cria um rascunho de configuração que o simulador testa antes da aprovação por outra pessoa. |
| F2-LAB-02/03 | ⬜ | Placar, IC, replay na UI, varredura de modelos. |
| F2-SUM-01 | ✅ | Resumo rolante mascarado, contando no orçamento do tenant. |
| F2-HO-01 | ✅ | Fora do horário, SLA com re-alerta, reabertura, roteamento. |
| F2-LGPD-01 | ✅ | `retention_sweep` diário. |
| F2-LGPD-02 | ✅ | Tela LGPD: busca, exportação JSON, correção e eliminação com confirmação digitada, tudo auditado. |
| F2-LGPD-03 | ⛔ | Pacote jurídico; o schema bloqueia `go_live` enquanto houver transferência `pending`. |
| F2-CHAOS-01 | ✅ | Intercalações em `tests/chaos` + teste de carga (`tests/load/load_pipeline.py`, resultados em `docs/runbooks/teste-de-carga.md`: 100 conversas, p95 de `run_turn` 0,75 s com LLM simulado, invariantes ok). |
| F2-PILOT-01 | ⛔ | Depende do go-live. |

## Fases 3–5

| ID | Status | Onde / observação |
| --- | --- | --- |
| F3-DATA-01 | ✅ / ⛔ | Import de dataset com prévia, checksum e rollback. **Tabelas no console** (`/console/datasets`): o pacote declara `DatasetSpec` (colunas, rótulos e opções vêm dos `Field` do modelo da linha; o núcleo não conhece o segmento); o tenant cadastra na grade ou importa .xlsx/.csv (aceita rótulos como cabeçalho e números no formato brasileiro), vê a prévia com diferenças e coluna calculada, ativa, exporta e volta a uma versão anterior. Autor, anotação e data de ativação (migração 0008); regra de duas pessoas opcional (`operations.datasets.two_person_activation`); o simulador usa o rascunho (`datasets.PREFER_DRAFTS`). Tabela de planos ganhou nome, grupo, parcela oficial (substitui o cálculo) e índice de reajuste. Falta o cliente cadastrar a tabela real. |
| F3-SIM-01 | 🟡 | Simulação determinística; ⛔ validar a fórmula com casos do cliente. |
| F3-GH-01 | 🟡 | `group_history` desligado. O importador de histórico de grupos ainda não faz parte do repositório. |
| F3-FORM-01 | ✅ | Formulário → handoff `hot_lead`. |
| F3-DASH-01 | ✅ | Tela Painéis (funil, atribuição, variantes, SLA, custo). |
| F3-OPT-01 | ⬜ | GEPA/DSPy. |
| F4-WA-01..03 | ✅ / ⛔ | Adaptador WhatsApp com mídia e **recibos de entrega** (entregue/lido/falha) ligados à campanha; falta testar com número real. |
| F4-FU-01 | ✅ | Fora da janela só template, e campanha exige consentimento. |
| F4-IMP-01 | ✅ | Importação CSV/XLSX pelo console com mapeamento, dry-run, commit e desfazer. |
| F4-CMP-01/02 | ✅ | Ciclo de vida do alvo (enviado → entregue → lido / falha / opt-out), **pausa automática** por taxa de opt-out ou falha (antes nunca disparava, porque os alvos não saíam de `queued`), código 131050 revoga o consentimento de marketing, tela de métricas por variante. |
| F5-CON-01 | ✅ | Connect via API + webhook assinado que move a posse. |
| F5-CH-01 | 🟡 | Webchat pronto; Messenger não. |
| F5-CR-01 | ✅ | Handover como sinal de humano. |
| F5-PKG-01 | 🟡 | `auto_insurance` mínimo. |
| F5-META-01, F5-FLOW-01, F5-COP-01 | ⬜ | — |

## Defeitos encontrados e corrigidos nesta rodada

1. O job de ingress passava sempre o adaptador do Instagram, e eventos de WhatsApp e webchat usavam as regras do canal errado.
2. A transcrição real nunca tinha funcionado: o gateway enviava o cabeçalho `Content-Type: None`. Agora há teste ao vivo.
3. O teto total do tenant (`pilot_total_cap_usd`), que é o que a recarga aumenta, não era aplicado; transcrição e resumo também não contavam no orçamento do tenant.
4. Os alvos de campanha nunca saíam de `queued`, então a pausa automática e as métricas não funcionavam.
5. Os links "Assumi" e "Devolver ao bot" do e-mail ao vendedor apontavam para uma rota inexistente.
6. O replay do spool apagava o arquivo antes de gravar os eventos, e um crash no meio perdia mensagens.
7. A API e os workers do compose de dev conectavam como superusuário, ignorando o RLS.
8. Dois envios simultâneos do formulário criavam dois consentimentos e dois handoffs.
9. Uma falha num tenant interrompia o tick periódico de todos os outros.
10. O retry de jobs travados chamava um método inexistente do procrastinate e falhava a cada minuto no worker.
11. Os testes rodavam no mesmo banco do app de dev, e o worker tentava processar centenas de tenants de teste. Agora os testes usam `agentplatform_test` (criado e migrado sozinho), e os jobs periódicos ignoram tenants `offboarded`.

## Ajustes de conversa (calibração)

- `respond@v3`: responde primeiro a dúvida, não reescreve explicação já dada e aprofunda com exemplo (só com valores ditos pelo cliente ou presentes nas fontes); a pergunta do fluxo é opcional.
- Pergunta do fluxo que o cliente ignorou não volta nas próximas `funnel.question_cooldown_turns` (padrão 2) mensagens do assistente.
- Detector de repetição configurável (`output_guard.repetition_rewrite_similarity`, padrão 0,8; antes fixo em 0,9).
- Diretriz `how_it_works` no pacote: explicação passo a passo com exemplo do dia a dia.

## Revisão independente (01/11/2026)

Quatro revisores leram o código sem receber as conclusões uns dos outros. O problema dominante era silêncio: cliente sem resposta e ninguém sabendo. Tudo abaixo foi corrigido e tem teste em `tests/chaos/test_silent_failures.py`, `tests/integration/test_handoff_return.py`, `test_review_lgpd_meta.py`, `test_review_ops.py` e `tests/unit/test_review_guards.py`.

**Silêncio**
- Turno sem `queueing_lock`: o retry colidia com o próximo turno e congelava a conversa (reproduzido no Postgres; o teste repete o cenário). Jobs com trava de fila (`close_batch`, `fetch_username`, `summarize`) não têm retry da fila: as varreduras os recriam.
- Erro inesperado no turno: `turns.status = failed`, handoff `technical_failure` e mensagem curta ao cliente. Parar continua valendo, e conversa com uma pessoa fica em silêncio.
- Varreduras por minuto: mensagens sem turno (`pending_sweep_after_s`), envios sem desfecho (`sending_orphan_after_s`, vão para reconciliação, nunca reenvio) e evento que falha 5 vezes vira `dead_letters`.
- Volta ao assistente (ADR-020), avisos sem handoff (`is_notice`) e escalonamento ao responsável.
- Aviso ao vendedor: log não conta como entrega em produção, próxima pessoa é tentada, ordem estável, `routing.seller_ids` respeitado e alerta `handoff_undelivered`.
- Token: só o veredito explícito da Meta pausa o canal; token válido no dia seguinte libera. Retenção e checagem de token rodam em transações separadas.
- Turno não roda se a resposta não pode sair (kill switch de canal, token inválido, antes do `go_live`). As mensagens ficam pendentes, e o pedido para parar é registrado mesmo assim.
- Envio: bolhas em ordem, `unknown` não sobrescreve `sent`, confirmação por eco entra no histórico, e `find_sent_message` só aceita mensagem posterior à tentativa.
- Gateway: uma nova tentativa em timeout, 429 e 5xx (`NodeModel.retries`); resposta sem o formato esperado vira `ModelCallError`; HTTP 402 vira parada de orçamento com alerta.

**Conversa**
- Palavras-chave de handoff só para o que apenas quem já é cliente diria; casam a partir do início da palavra. O classificador continua pegando o pós-venda real.
- Comparar com financiamento passa; chamar o produto de financiamento continua bloqueado (dataset `compliance-v1` ampliado).
- Números do perfil contam como fonte. Fora do horário, um novo pedido não repete mensagem nem handoff.
- Áudio: a transcrição vira o texto da mensagem; o que não dá para ouvir vai para `media_review`. Vídeo recebe uma nota em vez de turno vazio. Spam de comentário ficou mais estreito.

**LGPD e Meta**
- `inbound_events`: texto cifrado (`text_enc`) e removido ao fim do processamento. A migração 0011 limpa as linhas antigas, exceto comentários, que saem na retenção.
- Mensagem apagada ou editada pelo cliente é aplicada aqui (mensagem, evento, checkpoints e resumo).
- Parar: mais frases reconhecidas pela regra, e o vendedor é avisado se estiver na conversa. Máscara: saúde, RG por "identidade", agência e conta, endereço e CEP.
- Transcrição via OpenRouter com `provider.zdr`. Produção recusa `LLM_PROVIDER=openai`.
- Eliminação alcança eventos de comentário, `turns.trace`, `guard_results`, `tool_calls`, `curation_queue`, `lead_stage_history.evidence` e checkpoints. Retenção de checkpoints, `llm_calls` e `turns.trace`.
- Parser: `referral` dentro de `message`, `standby`, `comment_id` do Facebook Login, IDs numéricos e item ilegível que não derruba a entrega (quarentena `unreadable.jsonl`, com alerta).
- Recuperação de direct (`core/orchestration/dm_recovery.py`): a cada 5 min, lê só participantes e horário, e só abre conversa que o assistente pode ler. O `id` da API é igual ao `mid` do webhook (confirmado na conta de teste, 6 de 6).
- Tabela de erros por `(code, subcode)`: conta, destinatário, comentário, janela e dono da conversa. Só o que o vendedor pode resolver vira handoff.
- `/privacy`, `/data-deletion` e callbacks com `signed_request`, botão "Refazer lista".

**Produção**
- Ativação só com gate: `lab gate --level full` e `config-activate` (hash do documento). Rascunho do console: `lab gate --config-version`. Recarga e flags herdam a avaliação da versão ativa.
- Sem config ativa, produção não cai no YAML. `go_live` agora barra de fato: sem ele, nada chega a cliente.
- Postgres com rota para o S3, alerta `wal_archive`, `/actions/*` e rota do WhatsApp alcançáveis, senha padrão removida e tela Trocar senha. `pg_owner_password` só no `migrate`.
- Vigia externo (`HEARTBEAT_URL`, `/health/deep`). Alerta só é registrado depois de entregue. Imagem pelo `uv.lock`, CI publica em `ghcr.io`. `var/` no `.gitignore`.

**Teste real na conta de teste (01/11/2026):**

| Caso | Resultado |
| --- | --- |
| Pergunta de compra | Respondida em cerca de 10 s. Evento sem texto em claro, eco reconhecido e conversa continua com o assistente |
| Desfazer envio depois da resposta | Webhook `is_deleted` chegou; mensagem e evento original apagados |
| Desfazer envio antes da resposta | Nunca respondida; nenhum turno |
| Pedido de vendedor | Handoff, aviso ao cliente e alerta em até 1 min |
| Retomar pelo console | Volta ao assistente sem repetir o handoff (defeito achado ao planejar este teste) |
| Link `ig.me` com `ref` pelo navegador | Nenhum evento chegou, mesmo com `messaging_referral` ligado: falta testar pelo celular (T3) |
| Parar | Registrado; confirmação em 6 s (antes esperava a varredura e levava 36 s) |
| Quem parou e volta a escrever | Sem resposta e sem gasto de IA; vendedor avisado no minuto seguinte |
| Vendedor responde pelo Instagram na web | Eco desconhecido passa para HUMAN em 6 s; o cliente escreve e o assistente fica calado, sem gasto |
| Retomar com mensagem pendente | Responde em 5 s |
| Imagem | Baixada, lida por OCR e respondida |
| Editar mensagem | Só chegou depois de ligar `message_edit` no app. Antes da resposta, o assistente responde a versão editada; depois da resposta, a correção volta a ser respondida (typo não) |

O app de dev agora assina `messages`, `messaging_postbacks`, `comments`, `message_edit`, `messaging_referral`, `standby` e `messaging_handover`. O app de produção precisa do mesmo: há alerta `app_webhook_fields` para campo desligado.

**Continua dependendo de conta, servidor ou jurídico:** [docs/runbooks/go-live.md](runbooks/go-live.md).

## Configuração pelo console (02/11/2026)

Decisão em [ADR-022](plan/adr/ADR-022-configuracao-pelo-console.md). Migração `0014_config_console.sql`. Testes em `tests/integration/test_config_console.py`, `tests/contract/test_config_is_read.py` e `tests/e2e/`.

- **Telas novas:** Preparação (o que falta para atender), Assistente (identidade, instruções, exemplos, mensagens, horários, passagem ao vendedor), Regras (diretrizes, motivos de chamar o vendedor, paciência por estágio, ferramentas, frases proibidas), Modelos (com preço), Testar a base (o que a busca acha, e por quê), Usuários (convite, papel, desativar).
- **Configuração:** cada rascunho mostra o que muda em palavras, tem avaliação por botão, pode ser atualizado ou descartado. "Por que respondeu isso" em cada turno da conversa.
- **Base de conhecimento:** aviso de documentos em conflito, troca do modelo de busca, curadoria agrupando perguntas parecidas.
- **Defeitos corrigidos:**
  - Citar material "Só orienta": só o prompt impedia. Agora o guard bloqueia.
  - Aprovar rascunho antigo desfazia o que foi ativado depois.
  - Formulário aberto logo depois de uma aprovação usava a cópia em memória e desfazia a aprovação (achado no teste real).
  - Prompts e Orçamento davam erro em produção antes da primeira ativação.
  - Trocar o modelo de embedding quebrava a busca sem aviso.
  - 44 opções sem efeito, agora implementadas ou removidas.
  - Dev e o exemplo de produção usavam o mesmo nome de índice, com modelos diferentes.
  - O registro do turno nunca dizia qual FAQ foi usada.
  - Desativar usuário não encerrava a sessão aberta.
  - Número em ferramenta contava também as partes dele: um `R$ 67` inventado passava com `1546.67` na fonte.
  - A primeira mensagem podia sair sem dizer que é assistente, e uma resposta podia afirmar sem base.
  - `agent_budget_used_ratio` criava uma série por turno e nunca limpava: 459 de 978 linhas do `/metrics` em dev.

**Teste real em dev (02/11), só com dados sintéticos:**

| O que | Resultado |
| --- | --- |
| Formulário, rascunho, simulador, IA real | O rascunho listou só o que mudou, e a resposta aplicou a instrução do negócio |
| Convite, entrar, desativar com a sessão aberta | Funcionou; a sessão caiu |
| Avaliação rápida pelo botão, worker real | Passou em 1,5 s |
| Avaliação completa pelo botão, worker real, teto de US$ 0,12 | 5 min 24 s; parou no teto, como devia. Extração 10/10, busca 30/30 |
| Conjunto de respostas, 40 casos | `respond@v9`: 29/40 (73%), medido sem reescrever. `respond@v10`: 35/40 (88%), meta 85% |
| 8 cenários novos, uma vez cada | 8/8, com injeção de prompt, CPF oferecido e ameaça jurídica |
| Perfil `obs` contra a API real | `up=1`, 544 séries, painéis e 17 regras carregados |

**Não medido:** cenários 20 × 3 (pass^3), que custam cerca de US$ 0,50 e não cabem no orçamento de dev. Só 10 dos 20 rodaram, a maioria uma vez. A meta do gate completo ainda não foi vista passando. Os modelos de produção não rodaram: estes números valem para o modelo de dev.

**Monitoramento, o que difere do plano:**
- Sem Loki e sem Alertmanager, por memória. Alerta do Prometheus não avisa ninguém: fica em `/alerts` e no painel. Quem avisa continua sendo o alerta da aplicação e o `HEARTBEAT_URL`.
- Disco: alerta da aplicação em `operations.alerts.disk_used_percent` e métrica `agent_disk_used_ratio`, lidos do disco onde o contêiner está.
- `JobsFailed` dispara quando cresce, não com "DLQ > 0": o total só cai quando alguém reprocessa.
- Sem regra, porque a métrica não existe: atraso de fila acima de 30 s, webhook parado por 30 min, erros 190/80002/368 do canal, saltos de 3σ em handoffs ou bloqueios.

## Base de conhecimento pelo console (02/11/2026)

Decisão em [ADR-021](plan/adr/ADR-021-material-pelo-console.md). Migração `0013_knowledge_console.sql`. Testes em `tests/integration/test_knowledge_console.py` e `tests/e2e/`.

- **Base de conhecimento:** enviar PDF, leitura em segundo plano com OCR das páginas sem texto, revisão por trecho com alertas, tirar e devolver trecho, aprovar com uso, público, versão, validade e o documento que substitui, retirar.
- **Perguntas frequentes:** rascunho, aprovação, edição que tira de uso, validade, retirada. A resposta passa pelas mesmas regras das mensagens automáticas, mas pode ter valores.
- **Curadoria:** responder e criar a FAQ, anexar a uma FAQ existente, ligar a um documento, marcar fora do assunto, com link para a conversa.
- **`ingest`** não aprova nada nem chama provedor. **`bootstrap`** cria o índice da base.
- **Preparar antes de ativar:** Tabelas e material agora funcionam com a configuração em rascunho. Antes, em produção, as duas telas quebrariam sem configuração ativa, e ativar exige o gate, que lê o material.
- **Contato da empresa:** um PDF com o telefone, e-mail ou CNPJ da própria empresa pode ter esses dados trocados por marcadores, a pedido de uma pessoa. CPF e cartão bloqueiam sempre.

**Teste real em dev (02/11), com PDF sintético escaneado:** worker real, OCR real das 2 páginas em cerca de 4,5 s, trechos corretos e aprovação recusada pela chave de testes, como previsto.

**Provedor real:** gerar vetores ao aprovar e responder com o trecho e a FAQ foram testados em 02/11 com material inventado (ver "Primeira execução com o OpenRouter").

## Meta: Provedor de Tecnologia (03/11/2026)

Decisões em `docs/plan/adr/ADR-025-provedor-de-tecnologia.md`. O app de dev foi para o portfólio verificado e foi identificado como Provedor de Tecnologia. **Nenhum pedido foi enviado à análise.**

**Conferido depois da mudança de portfólio.** Token da Página, campos de webhook, assinatura da Página, permissões prontas para teste e configuração do Facebook Login ficaram iguais. **Não medido:** a entrega de uma mensagem real depois da mudança.

**Páginas públicas.** O texto de `/privacy` passou a tratar o cliente como controlador e a plataforma como operadora. Continua sendo rascunho até a revisão jurídica.

**Follow-ups.** A tela `Follow-ups` do console (`api/templates/followups.html`) mostra os contatos parados, os próximos envios, o histórico e as regras. Todos os perfis veem; só o responsável muda, e a mudança vira rascunho com aprovação.

- **Regras:** nome, ligada ou não, gatilho, etapas e até `followups.max_steps` tentativas, cada uma com tempo e texto. A tela recusa tempo abaixo de `followups.min_delay` e soma de tempos que passe da janela do canal, porque esse lembrete nunca sairia.
- **Lembrete de quem parou de responder (`no_reply`):** agora ligado por padrão, 4 horas depois da última mensagem do assistente, nas etapas do fluxo. O gatilho já existia, mas não funcionava: só valia depois da primeira mensagem, porque o registro da tentativa continuava lá depois da resposta da pessoa; só valia em turno que mudasse de etapa; e somava 3 horas ao tempo configurado. A mensagem seguinte do assistente agora reinicia a contagem. Depois que um lembrete chega ao outbox, não volta a sair naquela conversa.
- **Contatos parados:** o assistente falou por último, a conversa está com ele e a pessoa não saiu. O filtro padrão é `followups.stalled_after`. Com a janela fechada, nada sai sozinho: quem retoma é uma pessoa da equipe, pelo próprio canal.
- **Desligar vale para o que já estava agendado:** o `tick` passa a pular a regra ou a conta desligada e a tentativa removida da regra, que antes derrubaria o lote inteiro.

## Posts escritos no console (03/11/2026)

Decisões em `docs/plan/adr/ADR-024-posts-pelo-console.md`. **A publicação está desligada** (`operations.posts.publishing_enabled`) até a Meta liberar a permissão.

| O que dá para fazer | Onde | Depende de |
| --- | --- | --- |
| Escrever foto, carrossel, reel ou story, com legenda e arquivos, salvar, reordenar e excluir | Posts, Novo post | Nada: já funciona |
| Aprovar e publicar agora, ou agendar | Mesma tela | Publicação ligada e `instagram_content_publish` na conta |
| Ligar e desligar comentários de um post | Página do post | `instagram_manage_comments` |
| Apagar um post do Instagram | Página do post, só `tenant_owner` | Publicação ligada e `instagram_manage_contents` na conta |

A Meta não permite editar a legenda ou o arquivo de um post publicado, e as telas dizem isso.

**Onde está:** `core/posts/` (rascunho e publicação), `PostPublisher` em `core/channels/ports.py`, o job `post_publish` chamado a cada minuto pelo `tenants_tick`, `/pm/<token>` em `api/main.py`, e as tabelas de `migrations/sql/0015_post_publishing.sql`.

**Situações de um post:** `draft` → `approved` → `publishing` → `published`. Termina em `failed` quando é certo que nada saiu, ou em `unknown` quando a Meta não confirma: aí uma pessoa olha a conta e responde no console. Os dois aparecem em Posts e geram o alerta `posts_attention`.

**Medido.** Testes com Instagram falso e com as requisições conferidas por transporte simulado (`tests/integration/test_post_publishing.py`), inclusive resposta perdida, worker perdido e arquivo fora do horário. **Nunca rodou contra a Meta de verdade.** A divergência da documentação sobre apagar e sobre o limite diário está no plano `18` §1, e o primeiro uso segue o checklist do `go-live`.

**Ao atualizar um ambiente.** Rode a migração. Em produção, o `Caddyfile` e o `compose.prod.yaml` mudaram: `/pm/*` passa a ser público, uploads de post vão até 110 MB, e a API ganha o volume `api_uploads`.

## Sem formulário na conversa (03/11/2026)

Decisão do dono da conta: o assistente não manda formulário na conversa. O passo de pré-cadastro saiu do funil deste segmento, e o próximo passo agora é o vendedor.

| Antes | Agora |
| --- | --- |
| Perfil mínimo completo gerava um link de pré-cadastro e movia o lead para `pre_cadastro_enviado` | O lead fica em `pronto_para_proposta`, e o assistente pergunta uma vez se pode chamar o consultor (`ready_question` no pacote) |
| Lembrete em 4 horas sobre o formulário | Lembrete em 4 horas com a mesma oferta (`seller_offer_unanswered`). A resposta do cliente ou a chegada do vendedor param o lembrete, como antes |
| Pedir o formulário gerava outro link | Pedir formulário ou pré-cadastro, ou aceitar a oferta, vira `hot_lead` |
| Aceitar dependia do classificador | Uma resposta curta logo depois da oferta ("bora", "claro", "ok") é decidida pelo código (`short_yes`): ao vivo, o classificador perdeu 5 de 10 respostas assim. A oferta é reconhecida pelo que a pergunta diz (`ready_offer_pattern`), porque o modelo a reescreve: comparar com a frase fixa não reconhecia "Quer que eu chame o consultor..." |
| A oferta podia voltar a cada dois turnos | Enquanto a oferta estiver no histórico recente, o código não a pede de novo. O lembrete de 4 horas continua |
| Quem completava o perfil e dizia "só ano que vem" na mesma mensagem recebia o próximo passo | Fica na etapa em que estava, sem pergunta e sem lembrete |
| O texto do modelo podia oferecer o formulário | Oferecer, enviar ou pedir para preencher formulário, pré-cadastro, ficha ou dados é recusado na saída. Dizer que quem cuida disso é o consultor passa (`respond@v11`) |
| Posts mostravam pré-cadastros | Mostram leads quentes (`handoffs.reason = 'hot_lead'`). Um formulário enviado também gera esse motivo, então a contagem serve para qualquer pacote |

**O que continua no núcleo.** A ferramenta do link, a página `/f/`, a tabela `lead_forms` e a regra de retenção seguem disponíveis para um pacote que declare `form_sent_stage`. Nenhum pacote declara hoje. A migração não muda, e os painéis do Grafana com essa contagem ficam em zero.

**Ao atualizar um ambiente.** O pacote não conhece mais a etapa antiga, e um lead parado nela quebraria o turno: `update leads set stage = 'pronto_para_proposta' where stage = 'pre_cadastro_enviado'`. Depois, publique a configuração nova para valer `respond@v11` e o lembrete. Produção nunca teve lead, e o banco de dev já foi atualizado.

**Medido ao vivo, com texto inventado e os modelos de produção (US$ 0,012).** Em 16 respostas à oferta, o classificador acertou as 6 negativas e 5 das 10 positivas, o que levou à regra de `short_yes`. Depois dela, os 2 casos de resposta e as 2 conversas novas passaram, uma vez cada. A avaliação completa com `respond@v11` ainda não rodou, por falta de orçamento em dev.

## Calibragem com uma leitura ampla de conversas (02/11/2026)

Uma leitura ampla de conversas reais de atendimento mostrou o que o assistente ainda não sabia fazer. O texto de tudo abaixo foi escrito para isto: nada veio de uma conversa, e os números e achados da conta ficam na pasta privada.

| O que acontecia | Agora |
| --- | --- |
| Recusa educada ("por enquanto não, obrigado") recebia nota alta de pedido para parar, e o contato era descadastrado para sempre | `analyze_turn@v5` separa recusar de pedir para parar. A intenção `not_interested` agradece e não insiste |
| Quem dizia não ainda recebia a próxima pergunta do fluxo | `stop_asking_on` no pacote: com essas intenções ou objeções, o fluxo não pergunta nada naquele turno |
| Pedir videochamada ou reunião seguia na conversa | Entra em `continue_elsewhere`, travado como o WhatsApp. Só com frases completas: "estou em reunião" não chama ninguém |
| Formas comuns de "como começo" passavam direto | `ready_to_buy` ganhou essas formas. "Como funciona" continua sendo dúvida |
| Histórico de lances, restrição no nome e carta já contemplada podiam receber resposta sem base | Três gatilhos com o motivo `specialist_question` |
| Outro vendedor, parceria ou emprego eram tratados como cliente | Intenção `business_contact`: avisa o vendedor e não vende |
| Reação em story abria conversa | `is_shared_only`: resposta a story sem letra nem número não gera turno. Com palavras, segue como antes (ADR-019) |
| Comentar a palavra do vídeo não tinha modelo próprio | Modelo `carta`, com uma pergunta de sim ou não, para que um "sim" sozinho tenha sentido |
| Faltavam orientações para os momentos delicados | `financial_hardship` (não vende), `declined`, `already_has`, `commission_question`. `not_now` só fala em crédito menor se o problema for a parcela |
| Promessas e pressão de vendedor passavam pelo bloqueio | Novas frases proibidas: prazo até meia-noite, cota reservada, empresa que compra a carta, comprador garantido, ganho, investimento seguro, comparação com CDB ou CDI, sem consulta. "Sem entrada" exige a explicação da primeira parcela |
| As perguntas mais comuns não tinham resposta sugerida | 6 FAQs novas, sempre como rascunho: regulamentação, entrada, nunca ser contemplado, desistir, reajuste e crédito maior que o bem. O dono decide se valem para a conta |

**Medido com modelos de produção e texto inventado:** descadastro 21/21 (com 6 casos novos de recusa), 12 frases do classificador, 5 respostas e 6 cenários novos, uma vez cada. Nas frases, as 2 divergências foram corrigidas ou eram a etiqueta esperada no teste. Os testes sem gasto cobrem gatilhos, bloqueios e reação em story.

**Sem medir:** a avaliação completa com os casos novos, que precisa de mais orçamento antes de ir para produção.

## Oferta de plano escrita pelo código (05/11/2026)

A frase com crédito, prazo e parcelas vem da tabela e sai como está (`manifest.templates.plan_offer*`, `required_text` da ferramenta). Antes o modelo escrevia a frase e omitia a parcela depois da contemplação, mesmo com a regra no prompt. Se o modelo não copiar o texto, o código troca a resposta por ele e pela pergunta do turno; nesse caso, o que o modelo respondeu sobre outra dúvida se perde. O valor exato substitui o arredondado para cima (`entry_approx` saiu). A oferta termina em "Faz sentido pra você?", e o consultor é oferecido na mensagem seguinte, salvo se o fluxo ainda precisar de uma resposta. Mudou `respond@v12`. No primeiro teste real, o classificador leu o "sim" como pronto para comprar e chamou o consultor direto; a regra seguinte corrige isso no código.

## Só o lead quente vai ao consultor (06/11/2026)

A leitura de conversas que seguiram no WhatsApp (texto e áudios transcritos no computador, sem nada enviado para fora) mostrou que o vendedor gasta o tempo com quem só quer entender, e que as vendas vieram de quem perguntou como entrar, escolheu um valor ou pediu os documentos.

- **Um "sim" a outra pergunta não é decidir.** Se a última mensagem do assistente não ofereceu o consultor, uma resposta feita só de palavras de concordância (`short_yes`) ou das palavras da própria pergunta ("faz sentido") perde as intenções de lead quente, leia o classificador o que ler (`policy.yes_to_another_question`). A pergunta seguinte passa a ser "Quer que eu chame o consultor para você começar?", e só um sim a ela chama o consultor.
- **Sinais de fechamento novos:** "dar continuidade", "manda os papéis", "acho que vou entrar", "gostaria de iniciar" e variações. Uma negação logo antes ("não quero entrar") não chama o consultor (vale para todo gatilho de `hot_lead`).
- **Quem vai conversar em casa** recebe a oferta da tabela de novo, palavra por palavra, como resumo (`offer_again_keywords` faz a simulação rodar outra vez), sem pergunta empurrada e sem consultor (orientação `talk_at_home`).
- **Quando a pessoa diz quando pode começar** ("só consigo dia 5"), isso fica no campo `start_when` do perfil, que aparece no resumo do consultor. O assistente não promete voltar a escrever: no Instagram, a conta só responde até 24 horas depois da última mensagem da pessoa.
- **Sete rascunhos de perguntas frequentes** com o que as pessoas perguntaram antes de decidir: precisa ter o valor da carta, adiantar parcelas, parcela reduzida, lance embutido, quantos são contemplados, valor quebrado de carta, continuar pagando depois de contemplar. Sem números: o dono aprova e completa no console.

O que continua com o consultor: perguntas sobre a venda da carta (quem compra, por quanto, quando), que precisam de um texto revisado da empresa.

- **Consórcio ou guardar o dinheiro.** "Não vale mais a pena investir no CDB?" ia para o consultor: o pacote proíbe citar CDB/CDI na resposta e não havia orientação para a comparação, então as duas tentativas eram barradas. A orientação `vs_saving` (acima da de revenda) explica que consórcio é compra planejada, não investimento, sem nomear aplicação nem falar de rendimento; o classificador passa a ler essa pergunta como `compare_financing`, não como revenda. Rascunho de FAQ `vs_saving`.

## Quem envia responde pelos dados do arquivo (05/11/2026)

Um PDF com CPF, cartão, CNPJ, telefone ou e-mail continua bloqueado por padrão, mas quem envia pode assumir a responsabilidade, no envio ou na tela do documento bloqueado. Os valores são trocados por marcadores, e a auditoria registra quem enviou. Nomes e endereços não são detectados: a revisão por trecho continua sendo a proteção. Nenhuma opção envia os valores originais à base. A aprovação segue dependendo do OpenRouter.

## Importar tabela de um arquivo (04/11/2026)

Em **Tabelas > Importar arquivo**, a pessoa envia PDF, .xlsx, .csv ou .txt, a IA monta as linhas e elas aparecem no editor de sempre, sem salvar. Depois segue o fluxo existente: salvar rascunho, testar no simulador, ativar.

**Onde está:** `core/knowledge/table_import.py`, as rotas `/console/datasets/import`, `models.table_extract` (opcional; sem ele a tela avisa que está desligada), `prompts/table_extract@v1.j2` e `operations.datasets.file_read_max_*`.

**Regras:**
- **Chave de testes recusa o arquivo** antes de ler, como na aprovação de material: o envio à IA só funciona com `LLM_PROVIDER=openrouter`.
- **Arquivo proibido da conta** é recusado pelo hash do nome. CPF, cartão, CNPJ, telefone e e-mail são mascarados antes de ir à IA.
- **Número que não está escrito no arquivo** é descartado, e a prévia diz qual foi. Código ou nome com dígitos que não estão no arquivo também.
- **Número que está no arquivo, mas fora da linha do item,** fica em branco e é listado. Vale quando pelo menos dois números da linha coincidem com uma linha do arquivo; em layouts com um valor por linha, a checagem não se aplica e só a revisão da pessoa protege.
- **O arquivo não é guardado.** A auditoria registra hash, tamanho e contagens, sem o nome do arquivo.

**Medido com a chave direta, em PDF inventado (4 planos, uma ordem maliciosa no texto e um e-mail):** US$ 0,0002 por leitura, 3 a 5 s, os 4 planos lidos, a ordem ignorada e o e-mail mascarado. O modelo inventou, nas duas leituras, a parcela do plano que não tinha parcela, copiando um valor de outra linha; a segunda foi com a regra explícita no prompt. Foi isso que motivou a checagem por linha, testada com o mesmo caso em teste automatizado, sem nova leitura real.

**Limites:** imagem solta não é aceita. O limite de 8.000 tokens de saída dá para cerca de 80 linhas; acima disso, a leitura falha ou corta, e a planilha no modelo é o caminho. Não foi testado com PDF real, nem pelo navegador contra um servidor.

## Venda da carta e WhatsApp (02/11/2026)

A leitura de conversas de atendimento mostrou dois caminhos que o assistente não conhecia. O texto de tudo abaixo foi escrito para isto: nada veio de uma conversa.

- **Comprar a carta para vender depois:** agora é um objetivo reconhecido. A parcela sai da tabela: de qualquer plano, ou só dos marcados com esse objetivo, se a tabela tiver algum. Quem pergunta o resultado da venda (quanto ganha, em quanto tempo, se vale como investimento) vai para o vendedor, e esse gatilho pode ser desligado em Regras.
- **Promessas e pressão:** as respostas passam a ser barradas se falarem de lucro, rentabilidade, retorno certo, grandes chances de contemplação, poucas vagas ou prazo para decidir.
- **Continuar no WhatsApp ou por telefone, ou mandar um número:** chama o vendedor, sem opção de desligar. O assistente nunca pede o número.

**Medido com o modelo de produção, em textos inventados:** entender a mensagem 12/12 e descadastro 15/15. Numa conversa ponta a ponta com tabela inventada, ele reconheceu o objetivo, mostrou a parcela com o aviso obrigatório e, quando perguntaram quanto se ganha, chamou o vendedor sem prometer nada. Os novos cenários da avaliação completa ainda não rodaram.

**Falta a revisão jurídica** desse caminho antes de ir ao ar: está na lista do go-live.

## Console: novo visual e edição (02/11/2026)

**Visual:** verde-floresta sobre fundo cor de papel, com fontes do sistema (nenhum pedido a terceiros além do htmx). Modo escuro mantido. No celular, a caixa de entrada vira lista e o menu se recolhe. Os estilos ficam em `api/templates/_styles.html`.

**Agora dá para editar pelo console:**
- Pergunta frequente: lista com Editar, Aprovar, Retirar e Excluir em cada linha. Só se exclui o que não está em uso.
- Documento aprovado: nome, uso, versão e validade, sem sair de uso nem gerar vetor novo. Documento fora de uso pode ser excluído.
- Texto de um trecho: recusado se tiver CPF, cartão, telefone ou e-mail. Em documento aprovado, só esse trecho vai ao provedor para o vetor novo, e a chave de testes recusa.
- Usuário: nome, WhatsApp e e-mail de aviso (a Preparação apontava para Usuários, mas o campo não existia). Quem foi desativado pode voltar.
- Lead: estágio, inclusive desfazer venda marcada por engano, e um atalho da conversa para corrigir os dados.
- Diretriz do pacote: o texto, por conta. Apagar volta ao original.
- Lembretes: tela própria em `Follow-ups`, com regras, tempos, etapas e textos.
- `Assistente > Mais ajustes`: orçamento, passagem ao vendedor, condução, ritmo, comentários, mídia, proteção de dados, equipe, operadores de dados e entrada no ar, sempre como rascunho com aprovação.

**Fica de fora, de propósito:**
- Reverter descadastro: decisão jurídica. O estágio de quem pediu para parar não muda pelo console.
- Criar campanha: depende do WhatsApp aprovado, porque envia a pessoas reais com modelos aprovados pela Meta.
- Enviar mensagem pelo console: o vendedor responde no Instagram. Mensagem enviada é registro e não se edita.
- Canal: desconectar e conectar outro é operação da plataforma.
- Definição do segmento (estágios, slots, fluxo) e ajustes técnicos: seguem no pacote e no YAML.

**Botões que o servidor recusaria** foram tirados para Revisor e Vendedor, e o formulário de modelos deixou de perder a reserva que não oferece.

**Dados de dev:** 16 documentos e 3 perguntas de uma importação antiga estavam sem dono e por isso sem botões. Passaram para a conta de dev, sem mudar status nem texto. Nenhum código atual cria material sem dono.

## Primeira execução com o OpenRouter (02/11/2026)

Até aqui nenhuma chamada tinha passado pelo provedor de produção. A primeira execução mostrou que **produção não teria funcionado**, sem que nenhum teste apontasse:

| O que acontecia | Efeito | Correção |
| --- | --- | --- |
| O gateway enviava `provider.only: []`, que o OpenRouter lê como "nenhum provedor" | Toda chamada de texto e de busca falhava com 404 | Listas vazias ficam fora do pedido |
| `gemini-2.5-flash-lite` não tem rota com retenção zero | `summarize` e `comment_intent` falhariam sempre | Modelo fora da tabela e da configuração; alerta diário quando um modelo em uso perde a rota |
| O OpenRouter manda bytes em branco enquanto espera, e o limite do httpx conta só o intervalo entre leituras | `timeout_s` nunca disparava; um provedor parado segurava o turno até os 40 s | `asyncio.timeout` em toda chamada |
| A reserva só rodava dentro do OpenRouter | Sem reserva após estouro do nosso limite, ou quando ela recusa os parâmetros do primeiro modelo | O gateway percorre a cadeia, com os parâmetros de cada modelo |
| `opt_out_intent` sem definição no prompt | "Me resume em uma frase" recebeu nota 0,9 e o cliente foi descadastrado | `analyze_turn@v4`; dataset `core/opt-out-v1.jsonl` com meta de 100%; mensagem marcada como injeção não descadastra pela nota do modelo |
| `config-activate` aceitava qualquer avaliação aprovada, e a linha de comando rodava 10 cenários por padrão | Avaliação parcial liberava produção | Só a avaliação completa aprova |
| Conversa simulada que falhava voltava sozinha para o assistente | Gasto sem ninguém esperando | Canal `simulator` fica fora do retorno automático |

**Modelos escolhidos por medição** (todos com retenção zero; preços em US$ por milhão de tokens, entrada/saída):

| Etapa | Modelo | Reserva | Por quê |
| --- | --- | --- | --- |
| Entender a mensagem | `mistral-small-2603` (0,15/0,60) | `gemini-3.1-flash-lite` | 10/10; `gpt-5-nano` fez 4/10, sem extrair nada nos erros |
| Escrever | `deepseek-v4-flash` via Crusoe (0,14/0,28) | DeepInfra, depois `gemini-3.1-flash-lite` | 40/40; a DeepInfra é mais barata (0,09/0,18), mas levou 6 s contra cerca de 2 s |
| Revisar | `gpt-5-nano` (0,05/0,40) | `mistral-small-2603` | Família diferente da escrita |
| Resumo e intenção de comentário | `gpt-5-nano` | `mistral-small-2603` | Mais barato; funcionou nos dois com os prompts reais |
| Busca | `qwen3-embedding-8b` via DeepInfra | Outras rotas com retenção zero | De 1,0 a 1,4 s |

**Medido, sempre com dados inventados:**
- Antes das correções, com cada cenário uma vez: compliance 23/23 e 14/14, filtro de entrada 26/26, entender 9/10, respostas 40/40 (35/40 com o modelo de dev), busca 30/30, cenários 18/20.
- Depois das correções: descadastro 15/15, entender 10/10 e os 6 cenários afetados passaram. Turno: média de 9,9 s para 6,3 s; 5% mais lentos, de 21,7 s para 11,4 s; falhas técnicas, de 4 para 0.
- T22 e T23 pelo teste ao vivo: vetores, FAQ com outras palavras, resposta usando o trecho e a FAQ, troca do modelo de busca.
- Caminho principal, com conta descartável e tabela inventada: mostrou a parcela da tabela com o aviso obrigatório e chegou ao pré-cadastro, por US$ 0,003 a conversa inteira. O revisor recusou um rascunho por engano, e a reescrita saiu certa, com o turno em 13 s. Vale acompanhar a taxa de recusas indevidas.

**Não medido:** 20 cenários × 3 (fica para a configuração real da conta, porque a avaliação vale para o arquivo exato), reservas de escrita e de revisão com os prompts reais, material real.

**Para repetir:** `LLM_PROVIDER=openrouter pytest -m live tests/integration/test_live_openrouter.py -s`, menos de um centavo. Gasto real desta rodada: US$ 0,074 no OpenRouter.

**Dev continua na chave direta.** Para passar para o OpenRouter: `LLM_PROVIDER=openrouter` no `.env`, trocar a transcrição de `config/tenants/dev-consortium.yaml` (o modelo atual não tem rota com retenção zero) e rever `LLM_TOTAL_CAP_USD`, porque o contador `key:total` soma as duas chaves.

**`s_happy_vehicle` não passa em dev** enquanto as tabelas de exemplo estiverem em rascunho: sem tabela ativa não há parcela para mostrar, e a conversa não chega à proposta.

## Análise do que faltava (01/11/2026)

Cruzamento do plano com o código e o banco de dev.

**Achados que o status escondia**
- **Os modelos de produção nunca rodaram:** todas as chamadas de IA foram para a OpenAI, então o gate aprovado vale para outros modelos. Resolvido em 02/11 (ver "Primeira execução com o OpenRouter").
- **O gate é mais fraco que o plano:** 6 cenários uma vez (plano: 20, três vezes cada), sem `rag-v1` e `respond-v1`.
- **Indexação só com o provedor de produção:** o gateway recusa indexar documento com `LLM_PROVIDER=openai`. Com a chave de testes, as respostas sobre o produto dizem que vão confirmar com o vendedor.
- **Documentos e Curadoria só listavam:** o go-live dizia para carregar o material pelo console, o que não existia. Construído em seguida (ver abaixo).

**Feito nesta rodada** (`tests/integration/test_gap_fixes.py`, `tests/unit/test_holidays.py`, `tests/e2e/`)
- **Correção de dados** na tela LGPD: nome, telefone, e-mail e o que o assistente anotou. Tudo ou nada. Telefone de outra pessoa é recusado. Refaz o resumo. A auditoria guarda só os nomes dos campos.
- **Feriados:** Sexta-feira Santa, que muda todo ano, e `sending.holidays_local`, no lugar de `holidays_local_calendar`, que não fazia nada.
- **Revisão diária:** tela Revisão, registro por conversa (`conversation_reviews`, com texto mascarado, RLS, exportação, eliminação e retenção) e métrica `agent_review_findings_30d`.
- **Teste de restore mensal:** cron, tabela `restore_tests` e alertas `restore_test_failed` e `restore_test_overdue`.
- **Perguntas prontas (ice breakers):** tela Posts, com a mesma validação das respostas automáticas. Recusa publicar se o assistente não puder responder a quem tocar.
- **Guia do vendedor** em `/console/help`. O teste garante que os botões citados existem.
- **E2E do console** com navegador real e servidor próprio, sem gasto: login, duas etapas, papéis, posse, trace, link do aviso, revisão, LGPD, Posts, formulário público, pausa geral e aprovação por duas pessoas. Achou um defeito real: a tela de conversa transbordava para os lados no celular.
- **Documentos contraditórios** e branch `main`, de onde o CI publica a imagem.

## Desvios do plano

1. **Debounce no Postgres em vez do Redis.** As mensagens `pending` são o buffer durável; o Redis ficou só para rate limit.
2. **Ferramentas de domínio chamadas pelo código, não pelo LLM**, o que deixa verificável a proveniência dos números.
3. **Campos novos no manifest do pacote:** sinônimos de slot, `auto_transitions`, `flow_stages`, `form_sent_stage`, `form_resend_keywords`, `ready_question`, `ready_offer_pattern`, `short_yes`, `intent_hints`, `lead_form_labels`, `scope`.
4. **Compliance efetivo = união do tenant com o pacote** (`resolve_with_package`).
5. **`allow_same_family_judge`** só em `environment: dev`.

**Funil sem pré-cadastro (03/11/2026).** Os documentos `04`, `09`, `13`, `14` e `15` do plano ainda descrevem o formulário como próximo passo do consórcio. Vale a seção "Sem formulário na conversa".
6. **Provedor OpenAI direto em dev**, teto rígido de US$ 1,00 e só dados sintéticos; produção usa OpenRouter com ZDR.
7. **Kill switch de capacidade "agente" não gera turno** (economiza LLM). As mensagens que chegam durante a pausa ficam pendentes e são respondidas no próximo turno, depois da retomada.
8. **`silent_update` não atualiza perfil nem resumo** (plano 04/05): com o vendedor na conversa o turno não gasta LLM, só consome as mensagens. Elas continuam no histórico que o agente lê quando volta.
9. **Regras de comentário em tabelas operacionais, não na configuração versionada** (ADR-018). O plano original já previa a tabela `comment_rules`; a implementação tinha colocado as regras na configuração.
10. **O assistente não atende mais todo direct da conta** (ADR-019): o plano original supunha uma conta só de atendimento.
11. **Handoff causado por falha nossa volta sozinho ao assistente** (ADR-020): muda a decisão P5 do plano.
12. **OCR no worker, não em container sob demanda** (ADR-021): muda o ADR-015.
13. **`ingest` não aprova nem indexa** (ADR-021): o inventário só propõe os metadados.
14. **Sem rerank** (ADR-022): nunca foi construído, e as opções saíram. Volta se a avaliação de busca mostrar que faz falta.
15. **`triggers_enabled` virou `handoff.disabled_reasons`** (ADR-022): o que chama o vendedor é do pacote, e o dono só desliga o que não está travado.
16. **Gate no canal `simulator`** (ADR-022): não usa mais a conta real do tenant. Corrige o aviso antigo de parar os workers para rodar o gate.
17. **Repositório público** (ADR-023): o plano perdeu a análise do material e a avaliação do servidor, que ficam em `docs/privado/`.

## Como rodar

```bash
docker compose -f docker/compose.yaml --profile dev up -d      # postgres + redis
uv run agentplatform migrate && uv run agentplatform bootstrap
uv run agentplatform user-add --email aprovador@example.com --name Aprovador --role tenant_owner   # 2ª pessoa
uv run pytest                                                    # 745 testes, sem custo (banco agentplatform_test)
uv run agentplatform lab gate --level fast                       # sem custo
uv run agentplatform simulate                                    # usa o LLM (conta no teto)
docker compose -f docker/compose.yaml --profile dev --profile app up -d --build   # api + workers + console
```


## Dependências externas para o go-live

1. App Meta: verificação da empresa, App Review (Advanced Access; Human Agent não é pedido, o roteiro fica em `docs/privado/`), `META_APP_ID`/secret e o token real da página (via tela Canais).
2. WhatsApp: número da plataforma + templates `handoff_alert_v1` e `ops_alert_v1` aprovados.
3. Tabela de planos oficial e casos de simulação do cliente.
4. Revisão jurídica (contrato de operador, cláusulas ANPD, RIPD, encarregado) → libera `go_live`.
5. VPS dedicada, domínio e bucket S3 para o backup.
6. Revogar a chave OpenAI de dev, depois de passar dev para o OpenRouter. A chave do OpenRouter já existe, com limite próprio.
7. Repositório no GitHub para o CI rodar e publicar a imagem (GHCR).
8. (Opcional) Credenciais do Connect/Chatwoot.

## Ainda sem dependência externa (próximos passos)

1. `consortium/rag-v1` com o material real, depois de aprovado e com o OpenRouter. A avaliação de busca roda hoje sobre um corpus sintético.
2. Lab na UI (placar, varredura de modelos) e GEPA/DSPy.
3. Criar conta nova pelo console; hoje é o `bootstrap`, e a tela Preparação mostra o que falta depois dele.
4. Messenger; descrição de imagem por LLM (`llm_describe`).

### Ajuste de tom (2026-10-17)
- `respond@v4` (dev aponta para ela; prod continua na versão aprovada): tom de conversa (curto, acompanha o registro do
  cliente), pergunta inicial pode juntar objetivo e faixa de valor, apresentação de opções com parcela reduzida até a
  contemplação, nunca oferecer opções/links que não estejam em `tool_results`, instruções legíveis na nova tentativa após
  reprovação da revisão automática.
- Pacote: intenção `ready_to_buy` + gatilho `hot_lead` por palavras ("quero fechar", "manda o pix", "quero entrar"...),
  diretrizes `not_now`, `goal_tradeoff`, `pf_pj`, `ready_to_buy`; palavra-chave jurídica "processo" trocada por expressões
  específicas ("processo de adesão" disparava handoff jurídico).
- Tabela de planos aceita crédito + parcela oficial + parcela reduzida até a contemplação, sem
  prazo/taxa (validador exige parcela oficial ou prazo + taxa).
- Guard de repetição: além da similaridade de caracteres, sobreposição de palavras de conteúdo
  (`output_guard.repetition_word_overlap`, `repetition_min_words`) para pegar explicações parafraseadas.
- Lab: `customer_simulator@v2` (o cliente simulado não assume o papel do vendedor); o executor de cenários usa a conta
  `instagram` do tenant (pegava a conta do simulador). Com os workers de dev no ar, eles disputavam a fila e, sem token, marcavam a conta como inválida. Resolvido depois: o gate roda no canal `simulator` (ADR-022).
- Gate completo com v4: compliance 100%, input 19/19, slots 75–88%, cenários 5/6 (`s_happy_vehicle` instável: o cliente
  simulado pede o pré-cadastro sem informar valor e prazo).

### Modelos e tom (2026-10-30)
- A OpenAI desativou `gpt-4.1-nano` (404): `analyze_turn` passou para `gpt-4.1-mini` (8/8 na extração, ~1,4 s; `gpt-5-nano`
  minimal acertou 4/8 e com esforço "low" passou de 6 s); `output_judge` e `summarize` usam `gpt-5-nano`; reserva da resposta
  `gpt-5-nano`. A config ativa no banco ainda era a v8 (`respond@v3`): o `respond@v4` só existia no arquivo.
- `respond@v5` + `PackageManifest.reply_examples` (exemplos de tom no pacote, nunca fontes de número) + diretriz
  `how_it_works` curta. Config v10 ativa no dev.
- Gate: os cenários agora rodam com a config candidata (arquivo), não com a ativa do banco.
- Redação de logs preserva os argumentos do log de acesso do uvicorn; primeira conexão do Instagram recusa conta já ligada
  a outro registro (`account_in_use`).


### Parcela da tabela como gancho de venda (2026-10-30)
- Em vez de perguntar prazo, puxar o cliente com a parcela da tabela. A regra de números continua: o valor sai da tabela de planos ativa, nunca do modelo.
- Núcleo (sem termos de segmento):
  - `ToolSpec.proactive_when`: a simulação roda sem o cliente pedir quando um insumo dela é preenchido no turno.
  - `GuidelineSpec.when_tools`: diretriz disparada por resultado de ferramenta.
  - `SlotSpec.hint` e `SlotSpec.not_evidence`: "uma carta" não vira objetivo; antes caía em "outro bem móvel".
  - Pergunta do fluxo recém-feita e não respondida: o fluxo avança para a próxima obrigatória; opcional só depois das
    obrigatórias.
  - Saída: link criado no turno é obrigatório (`missing_link`; após a segunda tentativa o código anexa o link). O aviso
    oficial (`required_disclosures`) é inserido pelo código quando a resposta usa números de uma ferramenta com
    `disclaimer_key`.
  - Pedido explícito de simulação sem tabela ativa → handoff `simulation_request`.
- Pacote consórcio:
  - perfil mínimo = objetivo + valor ou parcela (prazo saiu do fluxo; a tabela já tem os prazos);
  - `simulate` aceita objetivo vazio (todas as modalidades: crédito mais próximo, depois a menor entrada);
  - `PlanOption` traz `entry_installment` e `entry_approx` (arredondada para cima, de R$ 10 em R$ 10);
  - diretriz `offer_from_table`.
- `respond@v6` (o aviso de valores estimados é do código), `funnel.question_cooldown_turns: 1`,
  `consortium.simulation_enabled: true` no dev (sem tabela ativa, não informa valores). Config v12 ativa.
- Gate completo 100% antes do último ajuste. O último ajuste (opcionais só depois das obrigatórias) é determinístico e
  coberto por testes; rodou só o gate rápido, por causa do limite de US$ 0,50 da chave.
- Teste ao vivo com tabela sintética em rascunho (descartada): "Olá" → "Uma carta" → "100k" mostra a parcela de entrada
  + aviso; "pra casa" mostra a parcela depois da contemplação + link do pré-cadastro.

### Conexão da Página e logs (2026-10-31)
- O log de requisições do httpx gravava a chave secreta do app e tokens do Facebook nas URLs das chamadas ao Graph: a
  URL chega ao log como objeto (`httpx.URL`), e o filtro só redigia texto. A redação agora roda na criação de cada
  registro (fábrica de `LogRecord`), vale para qualquer logger/handler (inclusive os que o CLI do worker cria depois de
  importar o app), redige argumentos não textuais e tracebacks, e cobre `fb_exchange_token`/`appsecret_proof`. Os logs
  antigos dos containers foram descartados.
- Retorno do Facebook Login: o callback devolve uma página que volta ao console pela mesma origem (o 303 chegava sem o
  cookie de sessão `SameSite=strict`, caía no login e perdia o resultado, ex.: `select_one_page`).
- Mensagens da tela de Canais: cada código de erro do Facebook Login virou uma instrução (`ui.OAUTH_ERROR`; teste garante
  que todo `OAuthError` tem frase), e o motivo de pausa do canal aparece por extenso.
- Inscrição da Página nos webhooks (`POST /{page}/subscribed_apps`), que não estava ligada: feita logo após conectar e
  conferida a cada 10 min (`periodic_subscriptions`; reinscreve se o nosso app sumir da Página). Campo `feed`
  (`META_PAGE_SUBSCRIBED_FIELDS`): a Página não aceita campos do Instagram (`comments`), e `messages` exige
  `pages_messaging`; os campos do Instagram vêm do App Dashboard. Página de dev inscrita em 2026-10-31.

### Automação de comentários por post e controle de conversas (2026-11-01)
Decisão em `docs/plan/adr/ADR-018-automacao-de-comentarios-por-post.md`. Migração `0009_comment_automation.sql`.

**Corrigido nos testes com contas reais:**
- **Pedido para parar não era registrado.** "Não quero mais receber mensagens, para de me mandar" não casava com as palavras-chave (só valem como mensagem inteira), e o assistente respondia "vou parar" sem gravar nada. Agora há padrões de frase (`compliance.opt_out_patterns`, monotônicos), a intenção com alta confiança é aplicada direto (`input_guard.opt_out_intent_apply_threshold`), e uma resposta que promete parar sem registro é bloqueada (`output_guard.opt_out_promise_patterns`).
- **A regra ativa respondia em todos os posts.** Agora o padrão da conta começa Inativo e nada ativa sozinho.
- **Falhas que o status escondia:** regra por anúncio nunca casava, gatilho por intenção nunca rodava, ocultar spam não fazia nada, e comentário de reclamação não avisava ninguém. As quatro funcionam agora.
- **Direct automático atrasava até 1 min:** o limite de private replies recarrega devagar e começa vazio depois de 1 h parado; a nova tentativa ficava para a varredura. Agora é agendada para o momento certo.
- **Quem entrava por comentário nunca era avisado de que falava com um assistente virtual.** O direct automático agora precisa dizer isso (validado ao salvar).
- **Pedido de atendimento aparecia como avisado** quando só tinha ido para o log. O console alerta enquanto e-mail e WhatsApp não estão configurados.
- O e-mail de pré-cadastro ao vendedor chegava com o resumo vazio.

**Novo:**
- **Telas Posts e Comentários** (vendedor e responsável, sem segunda aprovação): padrão Ativo ou Inativo, escolha por post, palavras, textos, vigência, nota e objetivo do post, modelos do pacote, responder comentários já feitos, funil por post até a venda, e a lista do que precisa do vendedor.
- **O que o agente sabe:** comentário, legenda e nota chegam ao prompt (`respond@v7`, `analyze_turn@v3`, bloco `<origin>`), e a mensagem automática entra no histórico. Legenda e nota não são fonte de números.
- **Controle de conversas:** o texto do vendedor é salvo (mascarado) e o agente o lê ao voltar. Comentário de quem está com o vendedor não recebe resposta automática e aparece para ele. Quem já está em conversa não recebe outro direct. A caixa de entrada mostra origem e @, e esconde as conversas do laboratório (`LAB-`).
- **Busca periódica** a cada 5 min (`media_sync`): lista os posts e lê comentários novos. Mesma chave do webhook, então um comentário nunca é processado duas vezes.
- **Anúncios, stories e lives:** anúncio segue a escolha do post que promove e aparece na lista ao receber o primeiro comentário. Resposta a story chega ao agente com a nota do vendedor. Live não recebe resposta pública.
- **LGPD:** comentários entram na exportação, na eliminação (também de quem só comentou, busca por ID ou @) e na retenção (`retention_days.comments`).

**Testado com contas reais em 01/11 (Instagram web, tabela de exemplo ativa só durante o teste, e-mail em receptor local):** vendedor assume por eco, cliente com vendedor sem resposta, retomada pelo console, pedido de humano, link "Assumi" do e-mail, comentário encontrado pela busca, resposta pública e direct, gatilho por palavra e por IA, contexto do post na resposta, objetivo do post corrigido pelo cliente, parcela da tabela, pré-cadastro, reclamação, spam, pedido para parar.

**Não testado com contas reais:** resposta do vendedor pelo app do celular, webhook real de comentários (depende do App Review), story, anúncio, live, áudio e imagem pelo Instagram, envio de e-mail por SMTP real. Estes têm só testes automatizados, com a Meta simulada.

### Quem o assistente atende no direct (2026-11-01)
Decisão em `docs/plan/adr/ADR-019-quem-o-assistente-atende-no-direct.md`. Migração `0010_dm_scope.sql`. Motivo: uma conta pequena costuma misturar clientes com amigos, grupos e reels compartilhados.
- **Painel (tela Posts):** só quem veio de comentário, anúncio ou story; também contatos novos; ou todos. Conta sem escolha fica na primeira opção.
- **Lista de quem já conversa com a conta:** montada ao conectar, só com participantes e guardada como hash. Enquanto não termina, ninguém novo é atendido.
- **Fora do escopo nada é guardado:** nem contato, nem conversa, nem conteúdo do evento. A exceção é o pedido para parar, registrado sem resposta.
- **Controle por pessoa:** "Nunca atender" e "Atender esta pessoa". Contato antigo que comenta vai para o vendedor, que pode liberar em um clique.
- **Sem texto:** reel compartilhado, menção em story e figurinha não geram resposta.
- **Testado com contas reais em 01/11, em conta sem conversas pessoais:** lista lida da Meta real; direct de contato antigo ignorado sem guardar nada; contato novo ignorado na opção 1 e atendido na opção 2; "Nunca atender"; contato antigo comentando, liberado pelo painel, com resposta pública e direct reais. As mensagens de direct do cliente foram simuladas por webhook assinado, com a identidade real.
- **Ainda não testado:** lista numa conta de cliente, mensagens de grupo pela API, reel compartilhado e menção em story chegando pela Meta (só com testes automatizados).
