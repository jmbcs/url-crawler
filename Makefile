.PHONY: install lint format types test cov bench smoke docker check

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
	docker build -t url-crawler .
	docker run --rm url-crawler --help

check: lint types test
