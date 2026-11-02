FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 AGENTPLATFORM_ROOT=/app SPOOL_DIR=/var/spool/agentplatform
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY packages ./packages
COPY migrations ./migrations
COPY alembic.ini ./
COPY prompts ./prompts
COPY config ./config
COPY evals ./evals
# exactly what uv.lock says: an image built today and one built next month run the same dependencies
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps .
RUN useradd --create-home --uid 10001 app && mkdir -p /var/spool/agentplatform && chown app /var/spool/agentplatform
USER app
EXPOSE 8000
