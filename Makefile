# Pillows-upload Makefile
# Standardizes common developer commands.

PYTHON ?= uv run
PYTEST ?= $(PYTHON) pytest
RUFF ?= $(PYTHON) ruff
TY ?= $(PYTHON) ty

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  install      Install the package in editable mode"
	@echo "  lint         Run ruff check (lint)"
	@echo "  format       Run ruff format"
	@echo "  format-check Run ruff format --check"
	@echo "  typecheck    Run ty type checker"
	@echo "  test         Run pytest"
	@echo "  check        Run lint + format-check + typecheck + test"
	@echo "  clean        Remove build/cache artifacts"

.PHONY: install
install:
	uv pip install -e .

.PHONY: lint
lint:
	$(RUFF) check src/ tests/

.PHONY: format
format:
	$(RUFF) format src/ tests/

.PHONY: format-check
format-check:
	$(RUFF) format --check src/ tests/

.PHONY: typecheck
typecheck:
	$(TY) check

.PHONY: test
test:
	$(PYTEST)

.PHONY: check
check: lint format-check typecheck test

.PHONY: clean
clean:
	rm -rf build/ dist/ *.egg-info/ .ruff_cache/ .pytest_cache/ .ty_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
