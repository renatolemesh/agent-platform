# ADR-018 — Automação de comentários configurada por post, no console

- **Status:** aceito (01/11/2026) · **Prioridades afetadas:** 2, 3, 5

## Contexto
O vendedor posta pelo celular e publica posts de naturezas diferentes: oferta, conteúdo educativo, cliente contemplado, pessoal. A regra única por conta (`comments.rules`, com escopo `account`) respondia em todos os posts: um amigo que comentasse "quero ir nesse churrasco" recebia resposta pública e direct. Escolher por post exigia o ID do post e uma nova versão de configuração com aprovação de duas pessoas, o que não cabe em quem posta todo dia.

O cliente também comenta de formas que a regra não tratava: em vários posts na mesma semana, com uma pergunta sem as palavras-chave, depois de já estar com o vendedor, depois de já ter comprado.

## Opções
1. Manter as regras na configuração versionada e só criar uma tela para editá-las.
2. Tabelas operacionais editadas no console, com validação automática dos textos ao salvar.
3. Ativar sozinho quando a legenda pedir para comentar ("comenta QUERO").

## Decisão
Opção 2. A opção 3 foi recusada: **nada ativa sozinho**.

- **Padrão da conta:** Ativo ou Inativo para todos os posts (`comment_settings`), começando em **Inativo**. Post inativo não envia nada.
- **Escolha por post** (`media_automations`): seguir o padrão, inativo, só registrar, só resposta no comentário, só direct, ou comentário + direct. Palavras, textos e vigência próprios; campos em branco herdam o padrão.
- **Sem segunda aprovação:** vendedor e responsável editam direto. A segurança vem de duas coisas:
  - **Validação ao salvar**, com as mesmas regras determinísticas da revisão de saída do assistente: frases proibidas, pedido de dados sensíveis, dados pessoais, links não liberados, se passar por pessoa, mais de uma pergunta. Valores e números são recusados nos textos automáticos, porque os números de produto só podem sair de fontes verificáveis (tabela, trecho recuperado, mensagem do cliente).
  - **Auditoria** de toda alteração, e o botão de pausar comentários continua valendo.
- **O direct automático precisa identificar o assistente** quando `persona.disclose_on_first_message` está ligado (`comments.disclosure_terms`), pois é a primeira mensagem ao cliente (compliance M4).
- **Comentários antigos nunca são respondidos sozinhos:** vale o momento em que o post ou o padrão foi ativado (`active_since`). Responder comentários já feitos é um clique explícito do vendedor, limitado aos 7 dias da Meta.
- **Todo comentário fica registrado** (`comment_actions`) com o que foi feito, e a tela Comentários mostra o que precisa do vendedor: reclamação, pergunta sem palavra-chave, cliente que já está com o vendedor, cliente que já comprou, interessado em post de "só registrar".
- **Por pessoa, não só por post:** quem já está em conversa não recebe outro direct de abertura (`comments.person_contact_window_days`). A resposta pública usa um texto próprio, que não promete um direct que não saiu; sem esse texto, não há resposta pública.
- **Contexto do agente:** comentário, legenda e nota do vendedor chegam ao prompt como dado (`<origin>`), mascarados. A legenda e a nota **não são fonte** de números nem evidência de slot; o comentário, por ser palavra do cliente, é. O objetivo definido no post entra no perfil com confiança baixa e cede ao que o cliente disser.
- **Os limites continuam na configuração versionada** (`comments.*`): padrões de spam e tema sensível, tamanhos, janelas, busca periódica. `comments.rules` e `comments.hide_spam` ficam só para versões antigas carregarem; são ignorados.
- **Webhook é o caminho principal; a busca periódica é rede de segurança** (a cada 5 min, só posts recentes cujo número de comentários mudou). As duas usam a chave `cmt:<id>`, então o comentário é processado uma vez, seja qual for a ordem. Ela também é o que funciona antes da análise da Meta, em que o webhook de comentários não é entregue.
- **Ocultar spam passa pelo outbox** (`hide_comment`): é opção do vendedor, respeita os botões de pausa e não vira mensagem na conversa.

## Consequências
- (+) O vendedor controla post a post sem depender de ninguém, e nada sai antes de ele ativar.
- (+) Nenhum comentário se perde: o que não recebe resposta automática fica visível.
- (−) Com o padrão Inativo, um post novo não responde até alguém ativar. É intencional: a prioridade 2 (nunca enviar mensagem imprópria) vem antes da 5 (conversão).
- (−) Sem aprovação de duas pessoas, um texto ruim que passe na validação sai para clientes até alguém corrigir. A auditoria registra quem alterou.
- (−) Anúncios e lives só aparecem na lista depois do primeiro comentário, pois a API não os lista junto com os posts.
