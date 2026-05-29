"""Data types for build operations."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


class ValidationResult(NamedTuple):
    """Result of RDF file validation."""

    is_valid: bool
    error_message: str | None


class FileValidationSummary(NamedTuple):
    """Summary of file validation results."""

    valid_files: list[Path]
    invalid_files: list[tuple[Path, str]]


@dataclass
class ComponentsConfig:
    """Configuration for UI components build."""

    source: Path
    entry: str


@dataclass
class MatrixBuildConfig:
    """Per-matrix build configuration within a package."""

    name: str
    path: Path  # Relative to package root
    spec_patterns: list[str]
    artifact_patterns: list[str]
    output_dir: Path
    components: ComponentsConfig | None = None
    # Path (relative to matrix dir) to the migrations directory. Defaults to "./migrations" so
    # authors don't need to declare it explicitly. Set to None when the matrix has no migrations
    # directory at all.
    migrations_dir: Path | None = None


@dataclass(frozen=True)
class PackageDependency:
    """A single declared cross-package dependency.

    The publisher/name pair identifies a package in the registry; the version range follows the
    same major.minor-only semver-range format used by ``engineVersion`` (see
    ``plgt.utils.version_range``). Persisted into the package archive's manifest so the registry
    can store declarations at publish time and the platform's resolver can walk the
    declared dep closure at install time.
    """

    publisher: str
    name: str
    version_range: str


@dataclass
class PackageConfig:
    """Package-level configuration from poliglot.yml."""

    name: str
    version: str
    engine_version: str
    project_dir: Path
    matrices: list[MatrixBuildConfig] = field(default_factory=list)
    # Optional cross-package dependencies declared in poliglot.yml's top-level ``dependencies``
    # map. Empty when the package has no declared deps. Order is preserved from the YAML so the
    # resolver / registry surface a stable iteration order in tests and logs.
    dependencies: list[PackageDependency] = field(default_factory=list)
    # Optional package metadata fields, all from poliglot.yml's ``package`` section. Threaded
    # into manifest.json verbatim; server-side validation is the authoritative gate for shape
    # rules. Empty / None when the publisher didn't declare them.
    description: str | None = None
    repository_url: str | None = None
    homepage: str | None = None
    license: str | None = None
    changelog: str | None = None
    tags: list[str] = field(default_factory=list)


class MatrixBuildResult(NamedTuple):
    """Result of building a single matrix within a package."""

    name: str
    output_dir: Path  # Directory containing assembly.ttl and artifacts
    matrix_uri: str | None
    total_triples: int
    valid_files_count: int
    total_files_count: int
    invalid_files: list[tuple[Path, str]]
    # Migration files included in the bundle, relative to migrations/ root. Defaults to an empty
    # tuple at the type-checker level (NamedTuple defaults must be immutable); the build service
    # converts to a list when populating.
    migration_files: tuple[str, ...] = ()


class PackageBuildResult(NamedTuple):
    """Result of building an entire package."""

    package_file: Path  # Path to package.tgz
    package_name: str
    package_version: str
    matrices: list[MatrixBuildResult]
