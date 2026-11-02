# ADR-010 — Notificador do vendedor

- **Status:** aceito (decisão do usuário em 09/10/2026: WhatsApp)

## Contexto
No handoff, o vendedor precisa de um aviso imediato com o mínimo de dados e com as ações "assumi" e "devolver ao bot". O inbox continua sendo o app do Instagram.

## Opções
1. **WhatsApp Cloud API** (oficial): template utility com botões de resposta rápida.
2. Telegram (bot gratuito, botões inline).
3. E-mail (links assinados).
4. API não oficial de WhatsApp: **proibida** (anti-padrão da §17).

## Decisão
Opção 1, com a 3 como fallback:
- **Número:** número próprio da plataforma (não o número pessoal do vendedor), com display name aprovado.
- **Opt-in:** consentimento do vendedor registrado em `consents` (`seller_alerts`).
- **Template:** `handoff_alert_v1` (utility, pt_BR), com botões cujo payload é assinado por HMAC.
- **Webhook:** o toque no botão chega como `type: "button"` e vira `SellerAction`.
- **Fallback:** e-mail com links assinados quando o template não estiver aprovado ou o envio falhar.
- **Alternativa:** o Telegram fica registrado como alternativa barata, implementável pelo mesmo `Notifier`.

## Consequências
- (+) O vendedor recebe o aviso onde já está.
- (−) Exige verificação de negócio, aprovação do template e meio de pagamento na conta WhatsApp.
- (−) Desde 01/10/2026, mensagens utility e de serviço dentro da janela são cobradas. São centavos de real por aviso; o valor exato vem do rate card oficial do Brasil, a confirmar.
- (−) Antecipa para a fase 1 parte do trabalho de WhatsApp da fase 4 (adaptador de webhook, assinatura, templates). Esse código é reaproveitado depois.
