.PHONY: up down db-reset test e2e check bench

TEST_DB := gridtiles_test
PSQL := docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres

up:
	docker compose up -d --wait --wait-timeout 60

down:
	docker compose down

db-reset:
	$(PSQL) -c "drop database if exists $(TEST_DB) with (force)" -c "create database $(TEST_DB)"
	uv run gridtiles init --db postgresql://postgres:postgres@localhost:$${GRIDTILES_DB_PORT:-5439}/$(TEST_DB)

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
