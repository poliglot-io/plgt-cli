# plgt CLI development commands

default:
    just --list

# Install / sync dependencies
sync:
    uv sync

# Run tests
test *args:
    uv run pytest {{ args }}

# Type check with mypy
typecheck:
    uv run mypy plgt

# Lint
lint:
    uv run ruff check
    uv run ruff format --check

# Build distribution packages
build-dist:
    uv build

# Clean build artifacts
clean:
    rm -rf build dist *.egg-info
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete

# Run the CLI
run *args:
    uv run plgt {{ args }}
