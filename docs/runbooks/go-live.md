# Go-live: da revisão independente ao primeiro cliente

Ordem para colocar o assistente na conta real. **Não conecte a conta real antes do passo 5.** A lista de contatos pode vir incompleta sem o Advanced Access, e os amigos do dono passariam a receber o assistente.

## 1. Repositório e imagem

- [ ] O repositório é público: nada do cliente entra nele (ADR-023). O `publish` só roda no branch `main`. No Windows, marque os scripts como executáveis antes do commit: `git add --chmod=+x scripts/*/*.sh docker/walg/*.sh docker/postgres/initdb/*.sh`.
- [ ] `uv sync --group dev` (ou instale o `pre-commit`) e `pre-commit install`: o hook bloqueia `.env`, PDFs e `docs/materiais/`.
- [ ] CI verde na `main`. O job `publish` gera `ghcr.io/<repo>:<sha>`, a imagem que o `deploy.sh` baixa.

## 2. Servidor e provedores

- [ ] VPS, DNS, S3 (WAL), SMTP e chave OpenRouter com limite e ZDR ativado na conta.
- [ ] `docker/prod.env` preenchido. A aplicação **não sobe** sem `OPS_ALERT_EMAIL` ou `OPS_ALERT_WHATSAPP`, e nem com `LLM_PROVIDER` diferente de `openrouter`.
- [ ] `HEARTBEAT_URL` de um vigia externo (por exemplo, healthchecks.io), com alerta se o ping parar por 5 min.
- [ ] Monitor externo em `https://<domínio>/health/deep`, que dá 503 sem banco ou sem worker.
- [ ] `LEGAL_CONTROLLER_NAME` e `LEGAL_CONTACT_EMAIL`. Sem eles, `/privacy` e `/data-deletion` dão 404.
- [ ] Deploy (`scripts/deploy/deploy.sh <imagem>`), e, depois do `bootstrap` da seção 3, `tenant_owner` com senha forte (`--seller-password`, ou gerada e mostrada uma vez), segundo aprovador (convite em Usuários) e TOTP.
- [ ] Agendar o teste mensal de restore: `sudo bash scripts/deploy/install-restore-cron.sh`. Sem um teste bom registrado, o alerta `restore_test_overdue` toca toda semana.
- [ ] Testar de verdade: WAL chegando no S3 (`pg_stat_archiver`), `bash scripts/backup/restore_test_monthly.sh` (registra o resultado), alerta chegando no seu e-mail, vigia avisando ao parar os workers.

## 3. Configuração de produção

- [ ] Copiar `config/tenants/prod-consortium.example.yaml` para `docker/tenants/<slug>.yaml`, **no servidor**, e preencher a persona, a empresa (`conversation.persona.company_name`), os sites (`conversation.allowed_urls`), os arquivos proibidos (`operations.knowledge.never_import_name_hashes`), o vendedor e `routing.seller_ids`, com os IDs reais de usuário. Esse arquivo nunca vai para o Git.
- [ ] `$C run --rm --no-deps migrate agentplatform bootstrap --slug <slug>`: publica como **rascunho**.
- [ ] Tabela real no console (Tabelas). Tabelas de exemplo nunca ficam ativas.
- [ ] Material real: coloque os PDFs em `docker/materiais/`, no servidor, e rode `$C run --rm --no-deps migrate agentplatform ingest --slug <slug> --dir /app/materiais`. Um `knowledge_inventory.yaml` na mesma pasta propõe os metadados e pode proibir arquivos pelo hash do nome (exemplo no pacote). Traz tudo para revisão, sem aprovar nada e sem chamar provedor, com as FAQs do pacote como rascunho. O que vier depois entra por Base de conhecimento.
- [ ] O vendedor revisa em **Base de conhecimento**: lê os trechos, tira o que tiver alerta e aprova. Depois, as respostas em **Perguntas frequentes**. Precisa de `LLM_PROVIDER=openrouter`, porque aprovar é o que envia o texto ao provedor; acompanhe o gasto em Orçamento. As telas funcionam com a configuração ainda em rascunho.
- [ ] Documento bloqueado por dado pessoal: o vendedor confere de quem são os dados. Se forem da empresa, ele envia de novo marcando a opção de substituir por marcadores. Nunca marque se houver dado de cliente.
- [ ] Feriados da cidade em `sending.holidays_local` (`MM-DD` todo ano, `AAAA-MM-DD` uma vez). Carnaval e Corpus Christi não são feriados nacionais: entram aqui se valerem na cidade.
- [ ] Em **Preparação**, confira o que falta. A lista lê o estado real da conta.
- [ ] Em **Assistente**, **Regras** e **Modelos**: nome, tom, instruções, mensagens, horários, feriados da cidade. Cada um vira um rascunho em **Configuração**.
- [ ] Em **Configuração**, clique em **Avaliação completa** no rascunho. Roda no worker, em conversas simuladas, e o custo aparece na tela. Com os workers no ar não há problema: nada chega a um cliente.
- [ ] Outra pessoa aprova. Em produção, só aprova se a avaliação daquele rascunho passou. Se aparecer "feito a partir de outra versão", use **Atualizar rascunho** e avalie de novo.
- [ ] Sem o console: `agentplatform lab gate --level full --slug <slug>` e `agentplatform config-activate --slug <slug> --experiment <id>`, que recusa se o arquivo mudou depois do gate.

Até aqui `go_live` está desligado: em produção, nada chega a um cliente, e só o simulador do console responde. Mensagens que chegarem ficam pendentes.

## 4. Meta

- [ ] Verificação da empresa.
- [ ] App Review (o roteiro fica em `docs/privado/`).

## 5. Conectar a conta real (só depois do Advanced Access)

- [ ] **Desligar as automações nativas** do Instagram (resposta instantânea, ausência, saudação de anúncio) até o teste T4.
- [ ] Meta Business Suite > Configurações > **Roteamento de conversas**: este app como principal, e Inbox/Business AI como secundários. Se não, as mensagens chegam como `standby` e tudo vai para o vendedor.
- [ ] App Dashboard > Webhooks > Instagram: ligar `messages`, `messaging_postbacks`, `comments`, `message_edit`, `messaging_referral`, `standby` e `messaging_handover`. Sem isso o evento nunca chega (visto no teste real de 01/11: edição não chegou). Há alerta `app_webhook_fields` para campo desligado.
- [ ] Conectar pelo Facebook Login na tela Canais. Conferir se `pages_messaging`, `instagram_manage_messages` e `pages_manage_metadata` estão na configuração do login.
- [ ] Conferir o número de contatos em Posts e comentários. Se parecer baixo, use **Refazer lista**.
- [ ] Escopo do direct: depois de a lista ficar pronta, escolher "também contatos novos" (`origin_and_new`). A conta nova começa em `origin_only`, e ninguém novo é atendido até a lista terminar.

## 6. Testes na conta real

Use uma conta de teste **sem papel no app**. Olhe o JSON em `inbound_events` e no log.

| # | Teste | O que confirma |
| --- | --- | --- |
| T1 | Direct de conta sem papel | Webhook chega com o app publicado |
| T2 | Clique em anúncio real | `referral` dentro de `message`; `lead_sources.ad_id` preenchido; o assistente sabe de qual anúncio veio |
| T3 | Link `ig.me` com `ref`, **pelo celular** | Evento `Referral`; o assistente abre a conversa. Pelo navegador nada chega (visto em 01/11) |
| T4 | Automação nativa ligada | O eco tem `app_id`? Se sim, coloque em `AUTOMATION_APP_IDS`. Se não tiver, mantenha desligada |
| T5 | Vendedor responde pelo celular | Eco desconhecido passa para HUMAN |
| T6 | Vendedor responde pela Business Suite | Idem; confira se chega eco ou `standby` |
| T7 | Vendedor responde pelo Instagram na web | Confirmado em 01/11 na conta de teste. Repetir com o app publicado |
| T8 | Vendedor envia áudio ou GIF | Eco sem texto não quebra e passa para HUMAN |
| T9 | Cliente desfaz o envio | Confirmado em 01/11 na conta de teste. Repetir com o app publicado |
| T10 | Cliente edita a mensagem | Confirmado em 01/11: pendente usa o texto novo, correção de mensagem respondida gera nova resposta. Repetir com o app publicado |
| T11 | Cliente envia áudio | Transcrição via OpenRouter com ZDR; formato aceito |
| T12 | Imagem, GIF, PDF, vídeo, reel compartilhado, sticker | Nenhum gera erro nem turno vazio |
| T13 | Menção em story e resposta a story | Origem correta |
| T14 | Grupo | A documentação diz que não é suportado: confirme que nada chega |
| T15 | Comentário com `comment_id` | Webhook de comentário ativo depois do Advanced Access |
| T16 | Comentário apagado antes da resposta privada | Erro 100/2534025 sem handoff |
| T17 | Parar a API por 30 min | Meta reentrega? O que faltar, a recuperação de direct traz em até 5 min |
| T18 | Parar a API por mais de 1 h | App desinscrito; `webhook_subscriptions` reinscreve em até 10 min; direct recuperado |
| T19 | Revogar a permissão do app | Erro 190: canal pausa, alerta sai, mensagens ficam pendentes e reconectar libera |
| T20 | Cliente bloqueia a conta | Erro 551 sem handoff |
| T21 | Conta nova abre o direct **pelo celular** e toca em uma pergunta pronta | Postback chega com a pergunta; o assistente responde e a origem fica como pergunta pronta. Publicar e retirar foram confirmados em 01/11 na conta de teste; o toque só existe no celular |
| T23 | Trocar o modelo de busca em Base de conhecimento, com o OpenRouter | Índice novo pronto, rascunho criado, busca segue no atual até aprovar, e depois responde pelo novo. Nunca testado com provedor real |
| T22 | Aprovar um documento e uma FAQ com o OpenRouter | Vetores gerados, custo em `llm_calls`, resposta usando o trecho e a FAQ. Nunca testado com provedor real: em dev a chave direta recusa, de propósito |

## 7. Ligar

- [ ] Jurídico: política de privacidade revisada, operadores com mecanismo de transferência definido (`subprocessors.transfer_mechanism`). O `go_live` é recusado enquanto houver `pending`.
- [ ] `go_live: true` pelo console, com aprovação por duas pessoas. Não exige novo gate, porque é mudança operacional.
- [ ] Revogar a chave OpenAI de dev e trocar o secret do app Meta.
- [ ] Entregar ao vendedor o guia em `/console/help`.
- [ ] Publicar as perguntas prontas em Posts, se quiser usar. A tela recusa antes do `go_live` e antes de a lista de contatos ficar pronta.
- [ ] Primeiro cliente com revisão diária na tela Revisão: a meta de violações é zero.

## O que está coberto só por documentação, sem teste real

Estes pontos seguem a documentação da Meta e só se confirmam nos testes acima: `app_id` no eco (T4), formato do `standby` em Instagram (T6), anúncio (T2) e reentrega (T17, T18). Na conta de teste foi confirmado que o `id` da API de conversas é igual ao `mid` do webhook, o que sustenta a recuperação de direct sem duplicar mensagem.
