# plgt

The authoring CLI for [Poliglot](https://poliglot.io), a semantic operating system that turns the things you do and the way you work into an executable program.

A *matrix* is a composable specification of your operating model — its capabilities, policies, and rules. `plgt` is the toolchain for authoring matrices: scaffolding a workspace, declaring matrix sources in Turtle, installing matrix packages from the public registry, validating them locally, and shipping them to a Poliglot workspace.

Status: **alpha**. Poliglot is in private beta; the CLI is stable enough to use day-to-day, but commands may evolve before 1.0.

## Installation

```bash
pip install plgt
```

Requires Python 3.12.x. [`uv`](https://docs.astral.sh/uv/) is the recommended install path:

```bash
uv tool install plgt
```

## Usage

```bash
plgt --help

plgt version              # Show version
plgt init                 # Initialize a new workspace
plgt auth login           # Authenticate via OAuth
plgt configure            # Configure workspace settings
plgt lifecycle            # Manage matrix lifecycle
plgt extension            # Manage extensions
plgt secrets              # Manage secrets
plgt ui                   # UI tooling (delegates to poliglot-ui)
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                       # install deps
uv run pytest                 # tests
uv run ruff check             # lint
uv run ruff format            # format
```

## Documentation

- CLI reference: <https://poliglot.io/docs/cli>
- Full docs: <https://poliglot.io/docs>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All contributors must sign the [Poliglot Contributor License Agreement](https://poliglot.io/cla) before their first PR is merged.

Bugs and feature requests go through GitHub Issues; security issues use [private security advisories](https://github.com/poliglot-io/plgt-cli/security/advisories/new) — see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE) · © Poliglot Inc.
