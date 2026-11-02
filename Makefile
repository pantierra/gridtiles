.PHONY: up down db-reset test e2e check

TEST_DB := gridtiles_test
PSQL := docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres

build/compose/martin.yaml:
	mkdir -p build/compose
	printf 'postgres:\n  connection_string: postgresql://postgres:postgres@db:5432/postgres\n  auto_publish: false\n' > $@

up: build/compose/martin.yaml
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
	$(MAKE) test
	$(MAKE) e2e
