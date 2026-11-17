.PHONY: install db seed dev test test-integration lint eval-check eval report up down

TEST_DATABASE_URL ?= postgresql://askwh:askwh@localhost:5436/warehouse_test

install:
	uv sync

db: ## PostgreSQL on localhost:5436, with the databases the tests and evaluation use
	docker compose up -d --wait db
	docker compose exec -T db createdb -U askwh warehouse_test 2>/dev/null || true
	docker compose exec -T db createdb -U askwh warehouse_eval 2>/dev/null || true

seed: ## load 90 days of simulated orders and production runs
	uv run askwh-seed

dev: ## the app on http://localhost:8010, with the simulated live feed running
	uv run uvicorn askwh.main:app --reload --port 8010

test: ## unit tests: no database, network or API key
	uv run pytest

test-integration: db ## all tests, including the ones that need PostgreSQL
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

eval-check: db ## free: every expected metric request must reproduce its reference SQL
	uv run askwh-eval check

eval: db ## paid: run the three approaches over the question set
	uv run askwh-eval run

report: ## free: rebuild results/report.md from the saved records
	uv run askwh-eval report --write

up: ## the database and the app in containers, on http://localhost:8010
	docker compose up -d --build --wait

down:
	docker compose down
