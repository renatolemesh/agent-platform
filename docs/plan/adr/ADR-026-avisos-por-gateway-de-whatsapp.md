# ADR-026 — Avisos ao vendedor por gateway de WhatsApp

- **Status:** aceito (09/11/2026, pelo dono). Substitui o item 4 das opções da ADR-010, que proibia API não oficial de WhatsApp.

## Contexto
A ADR-010 escolheu a WhatsApp Cloud API para avisar o vendedor. Ela exige um número próprio da plataforma, verificação do negócio, modelos aprovados pela Meta e um meio de pagamento. Até lá, o vendedor só vê os atendimentos no console, e um lead quente pode esperar sem ninguém saber.

Gateways como uazapi, Evolution API e Evolution Go mandam mensagens de texto por um número de WhatsApp conectado a eles, sem modelo nem aprovação. O dono já usa um deles para outros avisos.

## Decisão
- **O aviso ao vendedor e ao operador pode sair por um gateway.**
  - **Provedores:** `WHATSAPP_ALERT_PROVIDER` aceita `cloud`, que é o padrão, `uazapi`, `evolution` ou `evolution_go`.
  - **Configuração:** a URL do gateway vai na configuração do processo. O token fica num arquivo de segredo (`whatsapp_alert_token`), como os outros segredos.
- **Só para avisos a números da própria operação:** vendedor e operador. Nenhuma mensagem a cliente sai por gateway. O canal com o cliente continua sendo o oficial de cada plataforma.
- **Texto puro, com o mínimo de dados**, como na ADR-010: primeiro nome, motivo, estágio, resumo mascarado e link do console, que exige login. "Assumi" e "Devolver ao assistente" vão como links assinados, iguais aos do e-mail, porque gateway não tem botões de modelo.
- **O número do cliente nunca vai no aviso.** Quando o cliente deixa o WhatsApp, o aviso diz só que ele deixou; o número fica no console.
- **Em cadeia:** se o gateway recusar ou falhar, o aviso segue para o e-mail. A Cloud API continua implementada e volta a ser usada com `WHATSAPP_ALERT_PROVIDER=cloud`.

## Consequências
- (+) O vendedor recebe o aviso no WhatsApp já, sem esperar verificação nem aprovação de modelos.
- (−) **Gateway não oficial viola os termos do WhatsApp.** O número conectado pode ser bloqueado a qualquer momento, e ninguém responde por isso. Por isso:
  - o uso fica restrito a avisos internos;
  - o e-mail continua como fallback;
  - o painel continua mostrando os atendimentos.
- (−) **O token dá acesso ao número conectado**, então é tratado como segredo de produção e nunca vai para o repositório.
- (−) **A resposta "Assumi" não chega pelo WhatsApp:** o vendedor toca no link, que exige o mesmo segredo de assinatura dos e-mails.
