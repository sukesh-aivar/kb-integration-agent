# Makefile for the Knowledge Base Integration Agent.
# Targets wrap the common developer workflows from the spec (7.2, 7.5).

PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help setup test test-all eval run lint format typecheck docker-build clean

help:
	@echo "Targets:"
	@echo "  setup        Create venv and install runtime + dev dependencies"
	@echo "  test         Unit tests only, offline, no AWS credentials needed"
	@echo "  test-all     Unit + component tests (component marked @pytest.mark.aws)"
	@echo "  eval         Scored evaluation run (real scores in fake mode)"
	@echo "  run-fake     Full local pipeline on the sample pack, in-memory fakes (offline)"
	@echo "  run-aws      Full local pipeline against real AWS (needs credentials)"
	@echo "  lint         ruff check"
	@echo "  format       black + ruff --fix"
	@echo "  typecheck    mypy over src/"
	@echo "  docker-build Build the ARM64 container image"
	@echo "  clean        Remove caches and build artefacts"

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -r requirements-dev.txt
	@echo "Setup complete. Remember the system dep for python-magic:"
	@echo "  macOS: brew install libmagic   |   Debian/Ubuntu: sudo apt-get install -y libmagic1"

# Unit tests must pass with NO AWS credentials. The '-m not aws' filter lives in
# pyproject.toml addopts, so this is just the default pytest invocation.
test:
	$(BIN)/pytest

test-all:
	$(BIN)/pytest -m "aws or not aws"

eval:
	$(BIN)/python evals/run_evals.py --mode fake

run: run-fake

run-fake:
	$(BIN)/python scripts/run_local.py --mode fake

run-aws:
	$(BIN)/python scripts/run_local.py --mode aws

lint:
	$(BIN)/ruff check src evals tests sample_pack

format:
	$(BIN)/black src evals tests sample_pack
	$(BIN)/ruff check --fix src evals tests sample_pack

typecheck:
	$(BIN)/mypy src

docker-build:
	docker build --platform linux/arm64 -t kb-integration-agent:local .

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ build dist *.egg-info
