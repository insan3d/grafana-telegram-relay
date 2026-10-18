.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

export PATH := $(HOME)/.local/bin:$(PATH)

UV ?= uv
IMAGE ?= grafana-telegram-relay:local

.PHONY: all help setup hooks lint format test coverage check build image up down clean

all: check build

help: ## List available targets
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "%-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked development dependencies
	$(UV) sync --locked

hooks: setup ## Install commit and push hooks
	$(UV) run --locked prek install --config .pre-commit-config.yaml

lint: ## Check all project files, including untracked files
	git ls-files -z --cached --others --exclude-standard | xargs -0 -r $(UV) run --locked prek run --config .pre-commit-config.yaml --files

format: ## Format Python, TOML and Markdown
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .
	$(UV) run --locked taplo fmt
	$(UV) run --locked rumdl fmt --deny-config-warnings README.md README.ru.md AGENTS.md

test: ## Run offline tests
	$(UV) run --locked pytest -q

coverage: ## Run tests with coverage
	$(UV) run --locked pytest --cov=grafana_telegram_relay --cov-report=term-missing --cov-report=html

check: lint test ## Run linters and tests

build: ## Build and validate wheel and sdist
	$(UV) lock --check
	$(UV) build --clear
	$(UV) run --locked twine check --strict dist/*

image: ## Build the runtime Docker image
	docker build --tag $(IMAGE) .

up: ## Build and start the standalone service
	docker compose up --detach --build --wait

down: ## Stop the standalone service, keeping data
	docker compose down

clean: ## Remove generated build and test artifacts
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache .rumdl_cache .coverage htmlcov coverage.xml
