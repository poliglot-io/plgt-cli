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

## Quickstart

```bash
plgt --help                       # list every command
plgt auth login                   # authenticate against a workspace
plgt init my-matrix && cd my-matrix
plgt validate                     # run the validation pipeline
plgt build                        # produce the package tarball
plgt install --workspace dev      # ship it to a workspace
```

Each command has its own page in the [CLI reference](https://poliglot.io/docs/cli), auto-generated from this repo's source on every release.

## Documentation

- [CLI reference](https://poliglot.io/docs/cli) — every command's flags, defaults, and help text, auto-generated from this repo.
- [Full Poliglot docs](https://poliglot.io/docs)

## Contributing

Local setup, test commands, and the PR workflow live in [CONTRIBUTING.md](CONTRIBUTING.md). All contributors must sign the [Poliglot Contributor License Agreement](https://poliglot.io/cla) before their first PR is merged.

Bugs and feature requests: GitHub Issues. Security issues: [private security advisories](https://github.com/poliglot-io/plgt-cli/security/advisories/new) (see [SECURITY.md](SECURITY.md)).

## License

[Apache License 2.0](LICENSE) · © Poliglot Inc.
