# ADR-019 — Quem o assistente atende no direct

- **Status:** aceito (01/11/2026) · **Prioridades afetadas:** 1, 2, 3

## Contexto
Uma conta pequena costuma misturar clientes com a vida pessoal do dono: amigos, grupos, reels compartilhados e menções em story. O sistema respondia a qualquer direct da conta conectada, então o assistente falaria com amigos e família, e o eco do vendedor gravaria no nosso banco o que ele escreve para eles.

## Decisão
Canais marcados como `mixed_inbox` (hoje, o Instagram) têm um escopo por conta (`dm_settings.scope`), escolhido no painel:

- **`origin_only` (padrão):** só quem veio de comentário, anúncio, resposta a story ou pergunta rápida.
- **`origin_and_new`:** também contatos novos, sem conversa anterior com a conta.
- **`all`:** todos. Não recomendado em conta pessoal.

**Lista de quem já conversa com a conta.** Ao conectar, um job lê os participantes das conversas (`GET /{page}/conversations?fields=participants`), nunca as mensagens, em páginas retomáveis. Esses contatos ficam fora de `origin_only` e `origin_and_new`, mesmo que respondam a um story. Quando o dono escreve para alguém sem conversa aqui, a pessoa entra na lista.

**Falha para o lado seguro.** Enquanto a lista não termina, ninguém novo é atendido, em nenhum escopo restrito, e comentários ficam para o vendedor. Conta sem configuração conta como `origin_only` com a lista incompleta.

**Minimização.** Mensagem de quem está fora do escopo não é guardada: sem contato, sem conversa, e o evento fica sem conteúdo. Da lista, só guardamos um hash com chave do identificador (`contact_flags`). A exceção é o pedido para parar: ele cria o registro, sem nenhuma resposta, porque o `privacy_gate` vale em qualquer situação.

**Controle do vendedor.** "Nunca atender esta pessoa" tira o assistente da conversa na hora, vale também para comentários e para de guardar mensagens dessa pessoa dali em diante, inclusive as respostas do dono. O histórico anterior continua até a retenção ou um pedido de eliminação. "Atender esta pessoa" libera um contato da lista. A escolha do vendedor nunca é sobrescrita por fontes automáticas.

**Comentários.** Quem está na lista e comenta num post ativo não recebe direct nem resposta automática: o comentário fica em "Precisam de você". Reclamação continua indo ao vendedor.

**Mensagens sem texto.** Reel compartilhado, menção em story e figurinha não geram resposta. Numa conversa atendida, entram só no histórico (`kind = 'shared'`). Áudio, imagem e arquivo seguem normais.

**Conversa existente segue seu fluxo.** O escopo decide a entrada; depois disso valem a posse e as checagens de sempre.

## Consequências
- (+) Conectar a conta real não põe o assistente na vida pessoal do dono.
- (+) Nenhuma conversa pessoal é guardada.
- (−) Em `origin_only`, quem escreve direto continua só com o vendedor, como hoje.
- (−) Em `origin_and_new`, um amigo que escreva pela primeira vez recebe o assistente. O vendedor corrige com "Nunca atender".
- (−) Contato antigo que comenta interessado depende do vendedor, que pode liberá-lo em um clique.
- (−) A lista depende do que a API da Meta devolve. Conversas que ela não listar contam como contatos novos. Conferir na primeira conexão da conta real: o painel mostra quantas pessoas entraram.
