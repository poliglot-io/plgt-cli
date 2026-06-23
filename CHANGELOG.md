# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `plgt secret set` now writes secret values at a chosen **scope**. A new
  `--scope` option selects `workspace` (shared by everyone in the workspace,
  the default) or `principal` (private to a single principal), with
  `--scope-entity-id` naming the principal for the latter. The scope is sent
  with every value write.

## [0.1.0b10] — beta

### Fixed

- Update API response parsing to match the platform's nested foreign-reference
  shape. Related resources now arrive as nested objects, each carrying its own
  `uri`, instead of flattened `<resource>Id` / `<resource>Slug` fields. The CLI
  now reads:
  - extensions: `targetMatrix.uri`, `owner.id`, `owner.username`
  - lifecycle commands: `packageInstallation.id`, `parentCommand.id`
  - lifecycle events: `command.id`
  - registry namespace resolution: `publisher.slug`

  Previously these were read as top-level fields, so `plgt extension` and
  `plgt logs`/event views failed to parse current API responses.

### Changed

- `plgt secret list` / `plgt secret get`: the platform no longer returns
  per-secret `hasValue`, `lastAccessedAt`, or `accessCount`. Those columns and
  fields are removed; the commands now surface each secret's allowed scopes and
  its declaring matrix instead.

## [0.1.0b9] — beta

### Changed

- UI preview: launch Storybook to match the updated component contract.

## [0.1.0b8] — beta

### Fixed

- `plgt format`: preserve RDF-star reifier (`<<` / `>>`) and triple-term (`<<(` /
  `)>>`) delimiters. They were previously split (e.g. `>>` rewritten to `> >`),
  producing invalid SPARQL.

## [0.1.0b1] — beta

Initial release. The Poliglot platform is in private beta; the API surface
may change before 1.0.

[Unreleased]: https://github.com/poliglot-io/plgt-cli/compare/v0.1.0b10...HEAD
[0.1.0b10]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b10
[0.1.0b9]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b9
[0.1.0b8]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b8
[0.1.0b1]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b1
