FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 1000 crawler
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

USER crawler
WORKDIR /home/crawler

ENTRYPOINT ["url-crawler"]
