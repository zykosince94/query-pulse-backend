.PHONY: install dev test lint fmt typecheck openapi openapi-check migrate bootstrap demo down logs ai-eval fuzz soak

install:  ## Install all dependencies (incl. dev) into .venv
	uv sync

dev:  ## Run the API locally with reload (needs the app-db service and a .env)
	uv run uvicorn query_pulse.main:create_app --factory --reload

test:
	uv run pytest

lint:  ## Everything CI checks
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy query_pulse scripts tests
	uv run python scripts/export_openapi.py --check

fmt:
	uv run ruff check --fix .
	uv run ruff format .

openapi:  ## Regenerate openapi.json after changing the API
	uv run python scripts/export_openapi.py

openapi-check:
	uv run python scripts/export_openapi.py --check

migrate:
	uv run alembic upgrade head

bootstrap:
	uv run python -m query_pulse.accounts.bootstrap

demo:  ## Start the full local stack and wait until every service is healthy
	docker compose up -d --build --wait --wait-timeout 180

down:
	docker compose down

logs:
	docker compose logs -f app

ai-eval:  ## Score the AI assistant on the seven planted problems (needs QP_AI_EVAL_API_KEY, QP_AI_EVAL_MODEL)
	uv run python scripts/ai_eval.py

fuzz:  ## Schemathesis over every API operation (QP_FUZZ_EXAMPLES, default 20)
	uv run pytest tests/test_fuzz.py

soak:  ## One-hour soak test against the running stack (make demo first)
	uv run python scripts/soak.py --minutes 60
