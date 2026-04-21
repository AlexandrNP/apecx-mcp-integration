.PHONY: install install-dev lint format test smoke integration unit clean

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check src tests
	mypy src

format:
	ruff format src tests
	ruff check --fix src tests

test: smoke unit

smoke:
	pytest -m smoke -v

unit:
	pytest -m "not (integration or smoke or slow)" -v

integration:
	pytest -m integration -v

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
