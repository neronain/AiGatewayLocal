.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python
PIP   := $(VENV)/bin/pip

.PHONY: help venv install dev test lint fmt run mock seed docker-build docker-up docker-down docker-logs clean check

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python3 -m venv $(VENV) && $(PIP) install --upgrade pip

install: venv ## Install runtime dependencies
	$(PIP) install -e .

dev: venv ## Install runtime + dev dependencies
	$(PIP) install -e ".[dev]"

test: ## Run the test suite
	$(PY) -m pytest tests/ -v

lint: ## Lint
	$(VENV)/bin/ruff check app tests scripts

fmt: ## Auto-format and fix lint
	$(VENV)/bin/ruff format app tests scripts
	$(VENV)/bin/ruff check --fix app tests scripts

check: lint test ## Lint + test (what CI runs)

run: ## Run the gateway locally with reload
	$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

mock: ## Run a mock model backend on :8000
	$(PY) scripts/mock_backend.py --port 8000

seed: ## Seed a demo workspace (COURSE=CS101 STUDENTS=64123,64124)
	$(PY) scripts/seed.py --workspace $(or $(COURSE),CS101) \
	  --members $(or $(STUDENTS),6412345678) \
	  --models $(or $(MODELS),coding)

docker-build: ## Build the container image
	docker compose -f docker/docker-compose.yml build

docker-up: ## Start the stack
	docker compose -f docker/docker-compose.yml up -d

docker-down: ## Stop the stack
	docker compose -f docker/docker-compose.yml down

docker-logs: ## Follow gateway logs
	docker compose -f docker/docker-compose.yml logs -f gateway

clean: ## Remove caches and local databases
	rm -rf .pytest_cache .ruff_cache .mypy_cache data/*.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
