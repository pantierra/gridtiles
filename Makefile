.PHONY: up down test e2e check bench

up:
	docker compose up -d --wait --wait-timeout 60

down:
	docker compose down

test:
	uv run pytest

e2e:
	uv run pytest -m e2e

check:
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	uv run pytest -m ""

bench:
	uv run python -m tests.perf.bench
