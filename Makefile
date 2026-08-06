.DEFAULT_GOAL := help
SHELL := /bin/bash

STACK := victim-stack
VENV  := .venv
PY    := $(VENV)/bin/python

.PHONY: help bootstrap up down destroy ps logs tail health load inject reset status verify lint fmt test check

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Create the 3.11 venv for host tooling
	uv venv --python 3.11 $(VENV)
	uv pip install --python $(PY) -r requirements-dev.txt

up: ## Build and start the victim stack
	@test -f $(STACK)/.env || cp $(STACK)/.env.example $(STACK)/.env
	# Build the leaker too, so injection never has to build (and never has to
	# pass --build, which would drag its depends_on into a recreate).
	docker compose -f $(STACK)/docker-compose.yml --profile inject build
	docker compose -f $(STACK)/docker-compose.yml up -d
	@echo "waiting for api..."
	@until curl -sf http://localhost:8000/health >/dev/null; do sleep 1; done
	@echo "up: api http://localhost:8000  prometheus http://localhost:9090  toxiproxy http://localhost:8474"

down: ## Stop containers, keep volumes and logs
	docker compose -f $(STACK)/docker-compose.yml --profile inject down

destroy: ## Stop everything, delete volumes, images and log files
	docker compose -f $(STACK)/docker-compose.yml --profile inject down -v --rmi local
	rm -f $(STACK)/logs/*.log $(STACK)/.env

ps: ## Show container status
	docker compose -f $(STACK)/docker-compose.yml --profile inject ps

logs: ## Follow container stdout
	docker compose -f $(STACK)/docker-compose.yml logs -f --tail=50

tail: ## Follow the contract log files
	tail -f $(STACK)/logs/api.log $(STACK)/logs/worker.log

health: ## Hit each endpoint once
	curl -sS http://localhost:8000/health; echo
	curl -sS http://localhost:8000/widgets/1; echo
	curl -sS http://localhost:8000/widgets/1/cached; echo
	curl -sS -XPOST http://localhost:8000/jobs -H 'content-type: application/json' -d '{}'; echo

load: ## Generate background traffic (SECONDS=60)
	cd $(STACK) && ../$(PY) -m tools.load --seconds $(or $(SECONDS),60)

inject: ## Inject a failure (SCENARIO=pool-exhaustion|oom-crashloop|cache-latency|bad-config)
	@test -n "$(SCENARIO)" || { echo "usage: make inject SCENARIO=pool-exhaustion"; exit 2; }
	cd $(STACK) && ../$(PY) inject.py $(SCENARIO)

reset: ## Return the stack to healthy
	cd $(STACK) && ../$(PY) inject.py reset

status: ## Show current injection state
	cd $(STACK) && ../$(PY) inject.py status

verify: ## Prove every scenario has a distinct, visible signature (~10 min)
	cd $(STACK) && ../$(PY) verify.py $(ARGS)

lint: ## ruff check
	$(VENV)/bin/ruff check .

fmt: ## ruff format
	$(VENV)/bin/ruff format .

test: ## Unit tests for host tooling
	$(VENV)/bin/pytest -q

check: lint test ## Lint + unit tests
