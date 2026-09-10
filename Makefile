default: help

# --------------------------- MAKEFILE VARIABLES ---------------------------
FORMATTING_COLOR_YELLOW = \033[33m
FORMATTING_COLOR_BLUE = \033[36m
FORMATTING_END = \033[0m

DB_URL ?= postgresql+asyncpg://crawler:crawler@localhost:55432/crawler
TEST_DB_URL ?= postgresql+asyncpg://crawler:crawler@localhost:55432/crawler_test

# A local .env, when present, supplies every service variable through uv.
ENV_FILE := $(wildcard .env)
UV_RUN := uv run $(if $(ENV_FILE),--env-file $(ENV_FILE),)
WITH_DB = $(if $(ENV_FILE),,DATABASE_URL=$(DB_URL))
WITH_TEST_DB = $(if $(ENV_FILE),,URL_CRAWLER_TEST_DATABASE_URL=$(TEST_DB_URL))

.PHONY: default help install clean lint format types check test test-matrix cov test-service \
	smoke bench db-up db-down db-reset migrate migration api worker docker compose-up compose-down

##@ General

help: ## Show this help
	@printf -- "%s\n" \
	" " \
	"------ url-crawler ------------------------------------------------------------------------ " \
	" " \
	"A same-domain web crawler: a CLI that prints every page with its links, and an optional " \
	"job service (FastAPI, a worker and Postgres) that runs the same crawl in the background. " \
	"Usage: make <command> " \
	" " \
	"------------------------------------------------------------------------------------------ " \
	""
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@/ { printf "\n$(FORMATTING_COLOR_YELLOW)%s$(FORMATTING_END)\n", substr($$0, 5) } \
		/^[a-zA-Z0-9_.-]+:.*?## / { printf "  $(FORMATTING_COLOR_BLUE)%-14s ->$(FORMATTING_END) %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@echo

##@ Setup

install: ## Install every dependency from the committed lockfile
	@uv sync --frozen

clean: ## Delete caches and build output
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type d -name ".pytest_cache" -exec rm -rf {} +
	@find . -type d -name ".mypy_cache" -exec rm -rf {} +
	@find . -type d -name ".ruff_cache" -exec rm -rf {} +
	@find . -type d -name "*.egg-info" -exec rm -rf {} +
	@find . -type d -name "dist" -exec rm -rf {} +
	@find . -type f -name ".coverage" -exec rm -f {} +

##@ Quality

lint: ## Check style and formatting with ruff
	@uv run ruff check .
	@uv run ruff format --check .

format: ## Rewrite the code with ruff, fixing what it can
	@uv run ruff format .
	@uv run ruff check --fix .

types: ## Type-check with mypy in strict mode
	@uv run mypy

check: lint types test ## Everything CI runs, except Docker and the matrix

##@ Tests

test: ## Run the suite, offline and deterministic
	@uv run pytest

test-matrix: ## Run the suite on both Python versions CI uses
	@uv run --python 3.12 pytest -q
	@uv run --python 3.13 pytest -q

cov: ## Run the suite with coverage, failing under 75 percent
	@uv run pytest --cov --cov-report=term-missing --cov-fail-under=75

test-service: ## Run the service tests against a real Postgres
	@$(WITH_TEST_DB) $(UV_RUN) pytest tests/service -q

smoke: ## Crawl a real site, opt in, needs the network
	@uv run pytest -m network tests/smoke

bench: ## Sweep concurrency against the bundled fake site
	@uv run python scripts/bench.py

##@ Database

db-up: ## Start Postgres, and create the separate test database
	@docker compose up -d --wait postgres
	@docker compose exec -T postgres createdb -U crawler crawler_test 2>/dev/null || true

db-down: ## Stop Postgres, keeping its data
	@docker compose stop postgres

db-reset: ## Drop the Postgres volume and start again from empty
	@docker compose down -v postgres
	@$(MAKE) db-up

migrate: ## Apply every migration to the database
	@$(WITH_DB) $(UV_RUN) alembic upgrade head

migration: ## Generate a migration from the models, needs m="the message"
	@$(WITH_DB) $(UV_RUN) alembic revision --autogenerate -m "$(m)"

##@ Service

api: ## Run the HTTP API in this terminal
	@$(WITH_DB) $(UV_RUN) url-crawler-api

worker: ## Run one crawl worker in this terminal
	@$(WITH_DB) $(UV_RUN) url-crawler-worker

##@ Docker

docker: ## Build the CLI image and print its help
	@docker build --target cli -t url-crawler .
	@docker run --rm url-crawler --help

compose-up: ## Start the whole service stack, building first
	@docker compose up --build

compose-down: ## Stop the whole service stack
	@docker compose down
