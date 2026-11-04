# ADR-025 — O app da Meta como Provedor de Tecnologia

- **Status:** aceito (03/11/2026, pelo dono).

## Contexto
O plano previa, para o primeiro cliente, o caminho de app para a própria empresa, deixando Provedor de Tecnologia para a fase multi-cliente (`F5-META-01`). Esse caminho só vale para mensagens enviadas à própria empresa, e a conta do primeiro cliente não é da empresa dona do app. Sem acesso avançado, o app não recebe avisos de quem não tem função nele, ou seja, dos clientes de verdade.

## Decisão
- **O app é de Provedor de Tecnologia desde o primeiro cliente.** O painel avisa que essa decisão não pode ser revertida.
- **O app fica no portfólio verificado da empresa que opera a plataforma.** Cada cliente conecta a própria conta pelo Facebook Login for Business.
- **Nas páginas públicas, o cliente é o controlador dos dados das conversas, e a empresa que opera a plataforma é a operadora.** A operadora só é controladora dos dados de acesso de quem administra as contas. O texto é rascunho até a revisão jurídica.
- **O pedido segue o guia de apps para outras empresas.** O revisor usa as próprias contas, então precisa de um console acessível e de um login de teste.

## Consequências
- (+) Novos clientes usam o mesmo app, sem repetir a análise.
- (+) O pedido descreve a operação como ela é.
- (−) A empresa que opera a plataforma responde à Meta pelos dados de outras empresas e pode perder o acesso se a verificação cair.
- (−) O console precisa ficar acessível na análise. Antes disso, nenhuma conta com senha conhecida pode existir no banco desse ambiente.
- (−) A página de verificação de acesso da documentação diz que ela não é mais exigida, mas o painel ainda a mostra. Vale o painel.
