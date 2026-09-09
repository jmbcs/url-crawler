FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./

FROM base AS builder-cli

RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM base AS builder-service

RUN uv sync --frozen --no-dev --extra service --no-install-project

COPY src/ src/
COPY README.md ./
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra service --no-editable

FROM python:3.12-slim AS service

RUN useradd --create-home --uid 1000 crawler
COPY --from=builder-service /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

USER crawler
WORKDIR /home/crawler
COPY --from=builder-service /app/alembic.ini ./alembic.ini
COPY --from=builder-service /app/src/url_crawler_service/alembic ./src/url_crawler_service/alembic

CMD ["url-crawler-api"]

FROM python:3.12-slim AS cli

RUN useradd --create-home --uid 1000 crawler
COPY --from=builder-cli /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

USER crawler
WORKDIR /home/crawler

ENTRYPOINT ["url-crawler"]
