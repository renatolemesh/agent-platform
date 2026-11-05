# ADR-021 — Material gerenciado pelo console

- **Status:** aceito (02/11/2026). Substitui, no ADR-015, a parte do OCR em container sob demanda.

## Contexto
O plano diz que só `approved` chega à produção e que quem aprova é o vendedor. Na prática, o comando `ingest` aprovava pelo inventário do pacote, e as telas Base de conhecimento e Curadoria só listavam. Nenhum documento tinha sido aprovado por uma pessoa, e os textos passavam pela chave de provedor já na carga. Além disso, as telas de preparação exigiam configuração ativa, e a ativação em produção exige o gate, que lê o material.

## Decisão
- **Só o que uma pessoa aprova é usado.** A busca lê apenas documento `approved` e FAQ `approved`. O `ingest` não aprova nada: traz o material para revisão, com os metadados do inventário já preenchidos, e as FAQs do pacote chegam como rascunho do tenant.
- **O texto só sai do servidor na aprovação.** Ler o arquivo é local. Os trechos ficam sem vetor até alguém aprovar; só então vão ao provedor, com `purpose=ingest`, orçamento do tenant e fora de transação. Com `LLM_PROVIDER=openai` isso é recusado, na tela e no gateway.
- **O arquivo não fica guardado.** Cifrado em `kb_uploads` só até ser lido. Banco, por ser o único armazenamento durável que API e worker compartilham, e entra no backup.
- **Dado pessoal bloqueia e nada do texto é guardado.** O caminho é enviar uma cópia limpa, ou a pessoa que envia assume a responsabilidade pelos dados do arquivo, e CPF, cartão, CNPJ, telefone e e-mail são trocados por marcadores (`operations.knowledge.maskable_on_request`), com registro na auditoria. Até 05/11/2026, CPF e cartão bloqueavam sempre; a decisão mudou porque modelos com dados fictícios ficavam presos, e o valor original continua sem ser guardado.
- **Revisão por trecho.** A tela mostra os trechos e marca os que têm termo proibido, expressão sem o complemento ou dado pessoal. Tirar um trecho vale na hora e é reversível (`kb_chunks.excluded_at`).
- **OCR no worker do `ingest`**, com tesseract, que já está na imagem, uma página por vez e com limite (`max_ocr_pages`). Sai o container sob demanda do ADR-015: o vendedor envia um PDF escaneado pelo console e não tem como acionar um perfil de operação. Página a mais fica de fora, e a tela avisa.
- **Arquivos proibidos pelo pacote** são recusados pelo nome; fica só o hash, no arquivo da conta (ADR-023), antes de qualquer leitura.
- **Preparar a conta antes da primeira ativação:** `Runtime.tenant_for_setup` usa a versão proposta mais nova para limites e modelos. Nunca serve a um turno, então nada chega a um cliente por esse caminho.
- **Alterar uma FAQ aprovada a tira de uso** até nova aprovação: ninguém lê uma resposta que ninguém aprovou.

## Consequências
- (+) A trilha de quem aprovou o quê passa a ser real, e o gate de produção roda sobre o material que o cliente aprovou.
- (+) Aprovar de novo um documento retirado não custa nada: os vetores continuam lá.
- (−) A revisão é trabalho do cliente, e sem ela o assistente responde "vou confirmar" a quase tudo.
- (−) Tirar um trecho é tudo ou nada: não dá para editar o texto de um trecho.
- (−) OCR compete com os outros jobs da fila `ingest`, e por isso o limite de páginas.

## O que ficou de fora
Agrupar perguntas parecidas na curadoria (plano 06 §5: hoje só junta texto igual), alerta de conflito entre documentos, reindexar ao trocar de modelo de embedding.
