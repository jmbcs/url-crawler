.PHONY: install lint format types test cov bench smoke docker check \
	db-up db-down db-reset migrate migration api worker test-service compose-up compose-down

DB_URL ?= postgresql+asyncpg://crawler:crawler@localhost:55432/crawler
TEST_DB_URL ?= postgresql+asyncpg://crawler:crawler@localhost:55432/crawler_test

install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

types:
	uv run mypy

test:
	uv run pytest

cov:
	uv run pytest --cov --cov-report=term-missing

bench:
	uv run python scripts/bench.py

smoke:
	uv run pytest -m network tests/smoke

docker:
	docker build --target cli -t url-crawler .
	docker run --rm url-crawler --help

check: lint types test

# The service tests truncate their tables, so they get a database of their own.
db-up:
	docker compose up -d --wait postgres
	docker compose exec -T postgres createdb -U crawler crawler_test 2>/dev/null || true

db-down:
	docker compose stop postgres

db-reset:
	docker compose down -v

migrate:
	DATABASE_URL=$(DB_URL) uv run alembic upgrade head

migration:
	DATABASE_URL=$(DB_URL) uv run alembic revision --autogenerate -m "$(m)"

api:
	DATABASE_URL=$(DB_URL) uv run url-crawler-api

worker:
	DATABASE_URL=$(DB_URL) uv run url-crawler-worker

test-service:
	URL_CRAWLER_TEST_DATABASE_URL=$(TEST_DB_URL) uv run pytest tests/service -q

compose-up:
	docker compose up --build

compose-down:
	docker compose down
