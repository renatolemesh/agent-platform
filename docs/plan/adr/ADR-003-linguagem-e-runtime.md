# ADR-003 — Linguagem, runtime e console

- **Status:** aceito

## Contexto
A stack é fixa: LangGraph, Pydantic e FastAPI. O console precisa ser enxuto. TypeScript só entra com ADR.

## Opções
1. Python 3.12 em tudo, com console server-side em Jinja2 + HTMX.
2. Python no backend + SPA em TypeScript (React/Next) para o console.

## Decisão
Opção 1:
- Python 3.12 (`python:3.12-slim`) e `uv` com lock.
- FastAPI e Pydantic v2.
- LangGraph 1.2.x (MIT) e `langgraph-checkpoint-postgres` 3.1.x.
- psycopg 3, SQLAlchemy 2 (core) + Alembic e procrastinate.
- Console em Jinja2 + HTMX + CSS simples.

## Consequências
- (+) Uma linguagem, uma imagem e um pipeline de testes; o console reaproveita os modelos Pydantic.
- (−) UX mais simples que uma SPA. O construtor visual de fluxos (fase 5) pode exigir um ADR de TypeScript.
- `LANGGRAPH_STRICT_MSGPACK=true` (desserialização restrita dos checkpoints).
