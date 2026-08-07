set export

UV_CACHE_DIR := env_var_or_default("UV_CACHE_DIR", "/tmp/kisesh-uv-cache")
PYTHONDONTWRITEBYTECODE := "1"

default: check

check: lint typecheck coverage

format:
    uv run ruff format . --no-cache
    uv run ruff check . --fix --no-cache

package:
    uv build --no-sources

lint:
    uv run ruff format . --check --no-cache
    uv run ruff check . --no-cache

typecheck:
    uv run mypy --cache-dir=/tmp/kisesh-mypy-cache

live-close:
    KISESH_LIVE_TESTS=1 uv run python -m unittest tests.test_live_kitty_close -v

test:
    uv run python -m unittest discover -s tests -v

coverage:
    #!/bin/sh
    set -eu
    trap 'uv run coverage erase' EXIT
    uv run coverage erase
    uv run coverage run -m unittest discover -s tests
    uv run coverage combine
    uv run coverage report
