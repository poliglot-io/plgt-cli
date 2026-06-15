# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0b8] — beta

### Fixed

- `plgt format`: preserve RDF-star reifier (`<<` / `>>`) and triple-term (`<<(` /
  `)>>`) delimiters. They were previously split (e.g. `>>` rewritten to `> >`),
  producing invalid SPARQL.

## [0.1.0b1] — beta

Initial release. The Poliglot platform is in private beta; the API surface
may change before 1.0.

[Unreleased]: https://github.com/poliglot-io/plgt-cli/compare/v0.1.0b8...HEAD
[0.1.0b8]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b8
[0.1.0b1]: https://github.com/poliglot-io/plgt-cli/releases/tag/v0.1.0b1
