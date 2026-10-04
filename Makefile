.PHONY: install
install:
	uv sync --all-groups

.PHONY: test
test:
	uv run pytest -x tests

.PHONY: e2e
e2e:
	uv run pytest -v tests/test_e2e.py

.PHONY: lint
lint:
	uv run ruff check src/proper_sentry tests
	uv run ty check src/proper_sentry

.PHONY: lintfix
lintfix:
	uv run ruff check src/proper_sentry tests --fix

.PHONY: coverage
coverage:
	uv run pytest --cov-config=pyproject.toml --cov-report term-missing --cov proper_sentry tests
