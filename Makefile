# Single source of truth for the quality gate; CI runs `make check` too.
.PHONY: setup lint type test check audit
COV = --cov=nivesh_engine --cov=nivesh_adapters --cov-fail-under=85

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy

test:
	uv run pytest $(COV)

check: lint type test

audit:
	uv run pip-audit
