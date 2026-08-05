set export

UV_CACHE_DIR := env_var_or_default("UV_CACHE_DIR", "/tmp/kitty-workbench-uv-cache")

default: check

check: lint typecheck coverage

format:
    uv run ruff format .
    uv run ruff check . --fix

lint:
    uv run ruff format . --check
    uv run ruff check .

typecheck:
    uv run mypy

live-close:
    KITTY_WORKBENCH_LIVE_TESTS=1 uv run python -m unittest tests.test_live_kitty_close -v

test:
    uv run python -m unittest discover -s tests -v

coverage:
    uv run coverage erase
    uv run coverage run -m unittest discover -s tests
    uv run coverage combine
    uv run coverage report
