# ADR-023 — Repositório público

- **Status:** aceito (02/11/2026, pelo dono).

## Contexto
O dono decidiu publicar o código. O plano e os documentos descreviam o material interno do primeiro cliente, a marca, o servidor e contas de teste. O material do cliente não é só do dono para expor, e o que fica público pode ser copiado ou indexado mesmo que seja apagado depois.

## Decisão
- **Nada que identifique um cliente entra no Git:** nome, marca, sites, material, conversas, contas e servidores.
- **O que o produto precisa disso é configuração da conta:** `conversation.persona.company_name`, `conversation.allowed_urls` e `operations.knowledge.never_import_name_hashes` ficam no arquivo do tenant, que não é versionado (`docker/tenants/<slug>.yaml`, no servidor).
- **Arquivos proibidos pelo nome:** o hash de um nome previsível pode ser adivinhado, então o pacote só tem o de teste. O da conta fica no arquivo dela e no inventário do material. Sem eles, a recusa pelo nome não vale para aquele arquivo: a varredura de dados pessoais continua.
- **O inventário do material** fica junto do material (`knowledge_inventory.yaml`), não no pacote.
- **O que é local:** `docs/privado/` e `docs/materiais/` ficam fora do Git e da imagem Docker. Os originais do plano e o histórico anterior estão em `docs/privado/`.
- **Duas travas:** `tests/contract/test_public_repo.py` roda no CI e barra arquivo privado e endereço IPv4 público. O hook de commit lê uma lista local de termos, que não é publicada.

## Consequências
- (+) O código, os ADRs, os runbooks e os testes podem ser públicos.
- (−) O plano público perdeu a análise do material e a avaliação do servidor. Quem trabalha na conta real consulta `docs/privado/`.
- (−) A lista de termos só protege onde o hook está instalado; o CI não a conhece.
- (−) O histórico começa num único commit: o anterior tinha esse conteúdo e nunca foi publicado.
- (−) `group_history` fica sem dados até existir um importador: o que havia descrevia o relatório de um cliente.
