# ADR-024 — Posts escritos no console

- **Status:** aceito (03/11/2026, pelo dono).

## Contexto
A tela de Posts só configurava a resposta aos comentários. O dono pediu para criar e agendar posts pelo console. Publicar é irreversível e público: um erro não atinge uma pessoa, atinge todos os seguidores. A API da Meta não tem chave de idempotência, e a publicação pede uma permissão que o app ainda não tem.

O que a Meta permite (conferido em 03/11/2026, plano `18` §1): criar foto, vídeo, reels, carrossel e story; ligar e desligar comentários; apagar. Legenda e arquivo não mudam depois de publicados.

## Decisão
- **Nada sai sem uma pessoa aprovar.** O hash do conteúdo é gravado na aprovação e comparado antes de publicar. Mudar qualquer coisa exige voltar para rascunho e aprovar de novo.
- **Duas travas para ligar.** `operations.posts.publishing_enabled` começa desligado, e a conta precisa ter a permissão (`channel_accounts.scopes`). Sem as duas, dá para escrever e guardar rascunhos, mas não aprovar.
- **A publicação não passa pelo outbox.** O outbox entrega mensagens de uma conversa, e um post não tem conversa, posse, janela nem cliente. As verificações que valem aqui são refeitas a cada passo: kill switch (`posts`, canal e geral), hash, regras da legenda, arquivos, atraso e limite por dia.
- **Regras da legenda.** Valem as frases proibidas, os complementos obrigatórios, os dados que nunca se pede e os links liberados. Números e o contato da empresa são decisão de quem aprova, porque a regra de procedência existe para texto que ninguém revisa. CPF e cartão nunca passam.
- **Nunca há uma segunda tentativa às cegas.** Criar um container não publica nada, então pode ser repetido. Antes da chamada de publicação, grava-se `publish_started_at`. A partir daí, só se lê o status do container: `PUBLISHED` conclui, e qualquer outra coisa, depois da espera, vira `unknown`. Quem decide é uma pessoa olhando a conta. Só volta a tentar o que comprovadamente não saiu (falha de conexão ou limite da Meta).
- **Post atrasado não sai sozinho.** Depois de uma pausa, os agendados vencidos ficam parados à espera de uma pessoa (`late_after_min`), em vez de saírem todos juntos.
- **O arquivo fica cifrado e em pedaços, só até publicar.** A Meta busca o arquivo por URL, então existe `/pm/<token>`. O token é aleatório, só o hash é gravado, e o link só responde enquanto aquele post está em `publishing`. Publicado, o arquivo é apagado. Em pedaços, um vídeo não precisa caber na memória da API.
- **Apagar é mais restrito.** Pede a permissão própria `instagram_manage_contents`, publicação ligada e o papel `tenant_owner`, e nunca vale para anúncio.
- **Ligar e desligar comentários não depende da publicação.** Usa `instagram_manage_comments`, que o app já pede, e só acontece com o clique de uma pessoa.

## Consequências
- (+) Um erro de rede, um worker perdido ou uma resposta extraviada não geram post duplicado.
- (+) O post publicado entra no catálogo e recebe o padrão de comentários como qualquer outro.
- (−) Em `unknown`, alguém precisa olhar o Instagram e responder no console.
- (−) Os arquivos passam pelo banco e pelo WAL até serem publicados: o limite de vídeo (`max_video_bytes`) precisa acompanhar o proxy e o disco.
- (−) O upload vai para disco em produção (`TMPDIR` em volume próprio da API), porque `/tmp` é memória e conta no limite do container.
- (−) Nada disso foi testado contra a Meta de verdade: falta a permissão. A documentação diverge no método de apagar e no limite diário. O primeiro uso segue o checklist do `go-live`.
