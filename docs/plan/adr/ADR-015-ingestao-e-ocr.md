# ADR-015 — Ingestão de documentos e OCR

- **Status:** aceito; o OCR em container sob demanda foi substituído pelo [ADR-021](ADR-021-material-pelo-console.md)

## Contexto
O material de um cliente pode ter dezenas de PDFs, com páginas escaneadas, documentos internos, dados pessoais e duplicatas. A VPS tem 4 GB.

## Opções (versões em 09/10/2026)
| Ferramenta | Prós | Contras |
| --- | --- | --- |
| pypdf 6.20 / pdfplumber 0.11 | Leves; texto e tabelas nativos | Sem OCR; estrutura limitada |
| Docling 2.137 (MIT) | Layout, tabelas, ordem de leitura | Uso alto de memória relatado (16 GB falharam num escaneado de 184 páginas) |
| OCRmyPDF 17.13 + Tesseract `por` | OCR maduro em português | Pesado sob demanda |
| unstructured 0.27 | Muitos formatos | Dependências pesadas |
| marker | Bom layout | Restrição comercial nos pesos |

## Decisão
- **Texto nativo:** pdfplumber/pypdf.
- **PDF escaneado:** OCRmyPDF `-l por` em container sob demanda (perfil `tools`), com revisão humana.
- **Docling:** só em batch offline (máquina do operador ou job fora do horário com `page_range`), se o laboratório mostrar ganho no chunking por estrutura.
- **Varredura de PII:** obrigatória antes do índice.
- **Deduplicação:** por SHA-256.
- **Metadados obrigatórios:** status, visibilidade, público, vigência e aprovador.

## Consequências
- (+) Cabe na VPS; o OCR fica isolado.
- (−) A revisão humana do OCR e dos documentos é trabalho do cliente, e está no roadmap.
