# ADR-009 — Transcrição de áudio

- **Status:** aceito

## Contexto
Áudio é comum no Brasil. A estimativa é de 10% dos turnos com cerca de 30 s cada, ou ~40 min/dia. A transcrição precisa ser pt-BR, com ZDR (LGPD), latência menor que 10 s e custo de centavos.

## Opções (preços em 09/10/2026)
| Opção | Preço | Retenção |
| --- | --- | --- |
| OpenRouter `/api/v1/audio/transcriptions` com `whisper-large-v3-turbo` | ≈ US$ 0,0002–0,0007/min | `zdr: true` (sem escolha de provedor) |
| Gemini 2.5 flash-lite com `input_audio` (chat) | ≈ US$ 0,0006/min | ZDR via Google; transcreve e entende numa chamada |
| Groq whisper-large-v3-turbo direto | US$ 0,04/h | ZDR self-service |
| Deepgram Nova-3 | US$ 0,0043/min | `mip_opt_out` |
| OpenAI gpt-4o-mini-transcribe | ≈ US$ 0,003/min | ZDR só com aprovação de vendas |
| faster-whisper local (`small` int8) | VPS | 12–20 s para 30 s de áudio em 2 vCPU (estimado); disputa RAM |

## Decisão
OpenRouter `/audio/transcriptions` com `whisper-large-v3-turbo` e `zdr: true`. Fallback: Gemini flash-lite via `input_audio`. Tudo passa pelo `ModelGateway`, com orçamento. O laboratório mede o WER em 30 áudios reais anonimizados. O áudio original é baixado na chegada, porque a URL da CDN expira, e apagado em 30 dias.

## Consequências
- (+) Custo desprezível (≈ US$ 0,03/dia) e uma chave só.
- (−) Sem escolher o provedor no endpoint de STT; ZDR garantido pelo filtro.
