.PHONY: install run test lint format check clean

install:
	uv sync

run:
	uv run cli-chat

test:
	uv run pytest -v

lint:
	uv run ruff check src/ tests/
	uv run pyright src/
	uv run pylint src/cli_chat/

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

check: lint test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pyright/
