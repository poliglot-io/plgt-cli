# Contributing to plgt

Thanks for your interest in contributing.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

After `uv sync`, install the [pre-commit](https://pre-commit.com/) hook so `ruff check --fix` and `ruff format` run automatically on staged files before each commit:

```bash
uv run pre-commit install
```

Run the CLI in dev:

```bash
uv run plgt --help
```

## Reporting bugs

Open an issue with the **Bug report** template. Include the minimal reproduction, the `plgt` version (`plgt version`), your Python version, OS, and what you expected vs. what happened.

## Proposing changes

For anything beyond a small fix:

1. Open an issue first with the **Feature request** template so we can agree on scope.
2. Fork, branch from `main`, and make your change.
3. Run `uv run ruff check`, `uv run ruff format`, and `uv run pytest` locally — CI runs the same.
4. Open a PR against `main`. Fill out the PR template.

## Coding standards

- Strict typing; `ruff` is the linter and formatter, configured in `pyproject.toml`.
- Use `uv run …` not bare `python` / `pip` (per project convention).
- Tests use `pytest`. Co-locate tests under `test/<module>/test_*.py` mirroring the package layout.

## Releasing

Releases publish to PyPI as `plgt`. Maintainers cut a release by tagging `vX.Y.Z`; the publish workflow handles PyPI authentication. Do not publish from a local machine.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to abide by its terms.

## Contributor License Agreement

Before your first PR can be merged you'll need to sign the Poliglot CLA:

- Individual: <https://poliglot.io/cla/individual>
- Overview: <https://poliglot.io/cla>

The CLA is a one-time sign — it covers all current and future Poliglot OSS repos. Our CLA bot will leave a comment on your first PR with the signing link.

## License

By contributing you agree that your contributions will be licensed under the Apache License 2.0.

