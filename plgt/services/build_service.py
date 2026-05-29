"""Build utilities for package compilation.

This module provides the build workflow for compiling multi-matrix packages
into deployable bundles.
"""

from pathlib import Path

import yaml
from rich.console import Console

from plgt.core.exceptions import ValidationError
from plgt.models.build_types import (
    ComponentsConfig,
    MatrixBuildConfig,
    MatrixBuildResult,
    PackageBuildResult,
    PackageConfig,
    PackageDependency,
)
from plgt.services.archive_service import create_package_archive, create_tar_gz_bundle
from plgt.services.build_progress import (
    validate_files_with_progress,
)
from plgt.services.rdf_operations import (
    BuildError,
    create_output_directory,
    extract_matrix_metadata,
    merge_rdf_files,
    validate_rdf_file,
)
from plgt.services.script_expander import expand_script_refs
from plgt.services.ui_build_service import build_ui_if_needed
from plgt.utils.naming import validate_registry_slug
from plgt.utils.version_range import validate_range

_PATCH_RANGE_ERROR = (
    'engineVersion range must use major.minor only (e.g., ">=2.1 <3.0").\n'
    "Patch versions are not allowed in package compatibility ranges —\n"
    "workspaces always run the latest patch of their current minor."
)


def _validate_engine_version_range(engine_version: str) -> None:
    """Validate that ``engine_version`` is a major.minor-only compatibility range.

    Raises :class:`BuildError` with a user-facing message if the range is malformed
    or contains a three-component (patch) version. The server runs the same check
    at publish time as defense in depth.
    """
    try:
        validate_range(engine_version)
    except ValueError as e:
        msg = str(e)
        # The shared utility already rejects three-component bounds with a
        # "patch component" message. Surface the canonical user-facing copy
        # for that case so the CLI error matches the documented contract.
        if "patch component" in msg:
            raise BuildError(_PATCH_RANGE_ERROR) from e
        wrapped = f"Invalid engineVersion range '{engine_version}': {msg}"
        raise BuildError(wrapped) from e


def _parse_publisher_name(key: str) -> tuple[str, str]:
    """Split a ``publisher/name`` key into its components.

    Used for cross-package dependency keys in ``poliglot.yml``. Both segments must be non-empty
    and contain no whitespace; the format mirrors the registry's package identity which is
    keyed by ``(publisher, name)``.
    """
    if not isinstance(key, str) or not key.strip():
        msg = (
            f"Dependency key must be a non-empty 'publisher/name' string, got: {key!r}"
        )
        raise BuildError(msg)
    parts = key.split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        msg = (
            f"Dependency key '{key}' must be of the form 'publisher/name' "
            f"(e.g., 'widget/core')"
        )
        raise BuildError(msg)
    publisher, name = parts[0].strip(), parts[1].strip()
    if any(c.isspace() for c in publisher) or any(c.isspace() for c in name):
        msg = f"Dependency key '{key}' must not contain whitespace inside publisher or name"
        raise BuildError(msg)
    return publisher, name


def _parse_dependencies(raw_deps: object) -> list[PackageDependency]:
    """Parse and validate the ``dependencies`` map from ``poliglot.yml``.

    The map is optional. When present it must be a mapping of ``publisher/name`` -> semver range
    string. Each range must satisfy the same major.minor-only constraints as ``engineVersion``.

    Self-reference rejection (a package depending on itself) is intentionally
    not done here: the package's own publisher slug isn't something the local
    project authoritatively knows. CI/auth context determines the publisher
    at publish time, so the publish path is where self-reference can be
    checked against the actual identity. The CLI doesn't verify that
    declared deps exist in the registry either.
    """
    if raw_deps is None:
        return []
    if not isinstance(raw_deps, dict):
        msg = (
            "'dependencies' must be a map of 'publisher/name' -> semver range, "
            f"got {type(raw_deps).__name__}"
        )
        raise BuildError(msg)

    parsed: list[PackageDependency] = []
    seen_keys: set[tuple[str, str]] = set()
    for key, range_str in raw_deps.items():
        publisher, name = _parse_publisher_name(key)
        if (publisher, name) in seen_keys:
            msg = f"Duplicate dependency entry for '{publisher}/{name}'"
            raise BuildError(msg)
        seen_keys.add((publisher, name))
        if not isinstance(range_str, str):
            msg = (
                f"Dependency '{publisher}/{name}' version range must be a string, "
                f"got {type(range_str).__name__}"
            )
            raise BuildError(msg)
        try:
            validate_range(range_str)
        except ValueError as e:
            msg = (
                f"Invalid dependency range for '{publisher}/{name}' "
                f"('{range_str}'): {e}"
            )
            raise BuildError(msg) from e
        parsed.append(
            PackageDependency(publisher=publisher, name=name, version_range=range_str)
        )
    return parsed


def normalize_path(pattern: str) -> str:
    """Normalize a relative path by removing leading ./"""
    if pattern.startswith("./"):
        return pattern[2:]
    return pattern


_MIGRATION_FILENAME_PATTERN = "from-*.rq"


def discover_migration_files(
    matrix_dir: Path, migrations_subdir: str = "migrations"
) -> list[Path]:
    """Discover migration files in a matrix's migrations/ directory.

    Each migration is a SPARQL UPDATE script named ``from-<source-version>.rq``.
    Returned paths are sorted by filename for deterministic bundle layout. Test fixture
    directories (``from-<version>.test/``) are skipped — they ship as part of the test
    framework but are not loaded at execution time.
    """
    migrations_dir = matrix_dir / migrations_subdir
    if not migrations_dir.exists() or not migrations_dir.is_dir():
        return []

    return sorted(
        p for p in migrations_dir.glob(_MIGRATION_FILENAME_PATTERN) if p.is_file()
    )


def discover_rdf_files_in_pattern(project_dir: Path, pattern: str) -> list[Path]:
    """Discover RDF files in a spec pattern path.

    Args:
        project_dir: Project root directory
        pattern: Spec pattern (e.g., "./spec")

    Returns:
        List of RDF file paths (may be empty)
    """
    spec_path = project_dir / normalize_path(pattern)

    if not spec_path.exists() or not spec_path.is_dir():
        return []

    rdf_extensions = {".ttl", ".rdf", ".owl", ".nt", ".n3", ".jsonld"}
    rdf_files = []
    for ext in rdf_extensions:
        rdf_files.extend(spec_path.glob(f"**/*{ext}"))

    return sorted(rdf_files)


def load_project_config(config_path: Path) -> dict:
    """Load project configuration from poliglot.yml.

    Args:
        config_path: Path to poliglot.yml or poliglot.yaml

    Returns:
        Parsed configuration dictionary
    """
    with config_path.open() as f:
        return yaml.safe_load(f) or {}


def create_build_config(config_file: Path | None = None) -> PackageConfig:
    """Create build configuration from poliglot.yml.

    Args:
        config_file: Path to poliglot.yml (default: poliglot.yml in cwd)

    Returns:
        PackageConfig with resolved paths and matrix configurations

    Raises:
        BuildError: If config file not found or invalid format
    """
    # Determine project directory from config file location
    if config_file is None:
        project_dir = Path.cwd()
        yml_path = project_dir / "poliglot.yml"
        yaml_path = project_dir / "poliglot.yaml"
        config_file = (
            yml_path
            if yml_path.exists()
            else (yaml_path if yaml_path.exists() else None)
        )
    else:
        config_file = config_file.resolve()
        project_dir = config_file.parent

    if not config_file or not config_file.exists():
        msg = "poliglot.yml not found"
        raise BuildError(msg)

    config = load_project_config(config_file)

    # Validate package config format
    if "package" not in config:
        msg = "Configuration missing 'package' section"
        raise BuildError(msg)
    if "matrix" not in config:
        msg = "Configuration missing 'matrix' section"
        raise BuildError(msg)

    # Extract package info
    package_section = config.get("package", {})
    package_name = package_section.get("name")
    package_version = package_section.get("version")
    engine_version = package_section.get("engineVersion")

    if not package_name:
        msg = "Package name is required in package section"
        raise BuildError(msg)
    if not package_version:
        msg = "Package version is required in package section"
        raise BuildError(msg)
    if not engine_version:
        msg = "Package engineVersion is required in package section"
        raise BuildError(msg)

    # Lock down the shape of the package name at the build boundary so the
    # upload can't fail server-side with a confusing shape error after a long
    # compile. Server-side validation enforces the same regex; this is the
    # fastest-failing of the layered checks. ``package.publisher`` is
    # intentionally not accepted here — publisher identity comes from CI/auth
    # at publish time, not from poliglot.yml (see _parse_dependencies for
    # related rationale).
    try:
        validate_registry_slug("package.name", package_name)
    except ValueError as e:
        raise BuildError(str(e)) from e

    # engineVersion is a major.minor-only compatibility range. Validate at the build boundary
    # so malformed ranges or three-component (patch-level) versions are caught before the
    # package is uploaded. Registry-side re-validation runs the same check on publish so a
    # hand-crafted upload bypassing the CLI cannot land an invalid range either.
    _validate_engine_version_range(engine_version)

    # Parse the optional ``dependencies`` map, validate each range follows the same
    # major.minor-only syntax as ``engineVersion``, and reject duplicate keys. Self-reference
    # checking is deferred to publish time, where the publisher slug is known authoritatively
    # from CI/auth.
    raw_deps = config.get("dependencies")
    dependencies = _parse_dependencies(raw_deps)

    # Parse matrix configurations
    matrix_section = config.get("matrix", {})
    matrices: list[MatrixBuildConfig] = []

    for matrix_name, matrix_config in matrix_section.items():
        # Get matrix path (relative to project root)
        matrix_path_str = matrix_config.get("path", f"./{matrix_name}")
        matrix_path = Path(normalize_path(matrix_path_str))

        # Get spec patterns
        spec_patterns = matrix_config.get("spec", ["./spec"])
        if not isinstance(spec_patterns, list):
            spec_patterns = [spec_patterns]

        # Get artifact patterns
        artifact_patterns = matrix_config.get("artifacts", [])
        if not isinstance(artifact_patterns, list):
            artifact_patterns = [artifact_patterns]

        # Get output directory
        output_dir_str = matrix_config.get("outputDir", "./.matrix")
        output_dir = Path(normalize_path(output_dir_str))

        # Get components config
        components = None
        if "components" in matrix_config:
            comp_config = matrix_config["components"]
            components = ComponentsConfig(
                source=Path(
                    normalize_path(comp_config.get("source", "./src/components"))
                ),
                entry=comp_config.get("entry", "index.ts"),
            )

        matrices.append(
            MatrixBuildConfig(
                name=matrix_name,
                path=matrix_path,
                spec_patterns=spec_patterns,
                artifact_patterns=artifact_patterns,
                output_dir=output_dir,
                components=components,
            )
        )

    # Optional metadata fields. Pass through verbatim; server-side validation enforces the
    # shape (length caps, URL parsing, tag regex). The CLI only normalizes types — string
    # fields become None when blank, tags coerces to a list of strings.
    description = _optional_str(package_section.get("description"))
    repository_url = _optional_str(package_section.get("repositoryUrl"))
    homepage = _optional_str(package_section.get("homepage"))
    license_str = _optional_str(package_section.get("license"))
    changelog = _optional_str(package_section.get("changelog"))
    raw_tags = package_section.get("tags") or []
    if not isinstance(raw_tags, list):
        msg = "Package tags must be a list of strings"
        raise BuildError(msg)
    tags: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            msg = "Package tags must be a list of strings"
            raise BuildError(msg)
        tags.append(tag)

    return PackageConfig(
        name=package_name,
        version=package_version,
        engine_version=engine_version,
        project_dir=project_dir,
        matrices=matrices,
        dependencies=dependencies,
        description=description,
        repository_url=repository_url,
        homepage=homepage,
        license=license_str,
        changelog=changelog,
        tags=tags,
    )


def _optional_str(value: object) -> str | None:
    """Coerce a YAML scalar to a non-blank stripped string, or None.

    The publish path treats missing and blank-string the same way; this normalizes both at the
    build boundary so downstream code doesn't have to defensively .strip() everywhere.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"Expected string, got {type(value).__name__}"
        raise BuildError(msg)
    stripped = value.strip()
    return stripped or None


def build_matrix(
    progress, matrix_config: MatrixBuildConfig, project_dir: Path
) -> MatrixBuildResult:
    """Build a single matrix within a package.

    Args:
        progress: Rich progress instance for status updates
        matrix_config: Configuration for this matrix
        project_dir: Root directory of the package

    Returns:
        MatrixBuildResult with build output and metadata

    Raises:
        BuildError: If build fails
    """
    # Resolve matrix directory
    matrix_dir = project_dir / matrix_config.path

    if not matrix_dir.exists():
        msg = f"Matrix directory not found: {matrix_dir}"
        raise BuildError(msg)

    # Discover RDF files
    task = progress.add_task(
        f"[{matrix_config.name}] Discovering RDF files...", total=None
    )
    rdf_files: list[Path] = []
    for pattern in matrix_config.spec_patterns:
        rdf_files.extend(discover_rdf_files_in_pattern(matrix_dir, pattern))

    progress.update(
        task, description=f"[{matrix_config.name}] Found {len(rdf_files)} RDF files"
    )
    progress.remove_task(task)

    if not rdf_files:
        msg = f"No RDF files found in matrix {matrix_config.name}"
        raise BuildError(msg)

    # First validation pass to extract matrix URI for UI build
    task = progress.add_task(
        f"[{matrix_config.name}] Extracting matrix metadata...", total=None
    )
    validation_summary = validate_files_with_progress(
        progress,
        rdf_files,
        validate_rdf_file,
    )

    if not validation_summary.valid_files:
        msg = f"No valid RDF files found in matrix {matrix_config.name}"
        raise BuildError(msg)

    merged_graph = merge_rdf_files(validation_summary.valid_files)
    matrix_uri, _ = extract_matrix_metadata(merged_graph)
    progress.update(
        task,
        description=f"[{matrix_config.name}] Matrix URI: {matrix_uri or 'not found'}",
    )
    progress.remove_task(task)

    # Build UI components if configured
    if matrix_config.components:
        task = progress.add_task(
            f"[{matrix_config.name}] Building UI components...", total=None
        )
        ui_result = build_ui_if_needed(matrix_dir, matrix_uri)
        if ui_result:
            if ui_result.success:
                progress.update(
                    task,
                    description=f"[{matrix_config.name}] Built {len(ui_result.exports)} component(s)",
                )
            else:
                progress.update(
                    task,
                    description=f"[{matrix_config.name}] UI build failed: {ui_result.error}",
                )
        else:
            progress.update(
                task, description=f"[{matrix_config.name}] No UI components"
            )
        progress.remove_task(task)

        # Re-discover RDF files after UI build
        rdf_files = []
        for pattern in matrix_config.spec_patterns:
            rdf_files.extend(discover_rdf_files_in_pattern(matrix_dir, pattern))

    # Final validation and merge
    validation_summary = validate_files_with_progress(
        progress,
        rdf_files,
        validate_rdf_file,
    )

    if not validation_summary.valid_files:
        msg = f"No valid RDF files found in matrix {matrix_config.name}"
        raise BuildError(msg)
    if len(validation_summary.invalid_files) > 0:
        errors = "\n".join(
            f"  ✗ {file_path}: {error_msg}"
            for file_path, error_msg in validation_summary.invalid_files
        )
        msg = f"Found {len(validation_summary.invalid_files)} invalid RDF files in matrix {matrix_config.name}:\n{errors}"
        raise BuildError(msg)

    task = progress.add_task(f"[{matrix_config.name}] Merging RDF files...", total=None)
    merged_graph = merge_rdf_files(validation_summary.valid_files)
    matrix_uri, _ = extract_matrix_metadata(merged_graph)
    progress.update(
        task,
        description=f"[{matrix_config.name}] Merged {len(validation_summary.valid_files)} files ({len(merged_graph)} triples)",
    )
    progress.remove_task(task)

    # Create output directory
    output_dir = matrix_dir / matrix_config.output_dir
    create_output_directory(output_dir)

    # Discover artifacts
    task = progress.add_task(
        f"[{matrix_config.name}] Discovering artifacts...", total=None
    )
    artifacts_dirs: list[Path] = []
    for pattern in matrix_config.artifact_patterns:
        artifact_path = matrix_dir / normalize_path(pattern)
        if artifact_path.exists() and artifact_path.is_dir():
            artifacts_dirs.append(artifact_path)

    progress.update(
        task,
        description=f"[{matrix_config.name}] Found {len(artifacts_dirs)} artifact location(s)",
    )
    progress.remove_task(task)

    # Discover migrations/. Bundled flat under migrations/from-<src>.rq so the bundle loader
    # sees a stable layout regardless of where authors keep their sources.
    migration_files = discover_migration_files(matrix_dir)

    # Expand script:// refs into inlined SPARQL string literals before
    # serializing. Consumers never see script:// — only canonical TTL with the
    # SPARQL bodies as triple-quoted strings.
    #
    # Paths resolve relative to the matrix's primary spec/ directory
    # (where matrix.ttl lives by convention). This matches the validation
    # pipeline and the documented "Externalizing SPARQL with script://"
    # convention for the package format.
    task = progress.add_task(
        f"[{matrix_config.name}] Inlining script:// refs...", total=None
    )
    spec_dir = matrix_dir / "spec"
    script_base = spec_dir if spec_dir.is_dir() else matrix_dir
    try:
        expand_script_refs(merged_graph, script_base)
    except ValidationError as e:
        raise BuildError(str(e)) from e
    progress.remove_task(task)

    # Create tar.gz bundle for this matrix
    task = progress.add_task(f"[{matrix_config.name}] Creating bundle...", total=None)
    turtle_content = merged_graph.serialize(format="turtle")
    bundle_path = output_dir / "matrix.tar.gz"
    create_tar_gz_bundle(
        turtle_content,
        artifacts_dirs if artifacts_dirs else None,
        bundle_path,
        migration_files=migration_files if migration_files else None,
    )
    progress.update(
        task,
        description=(
            f"[{matrix_config.name}] Bundle created "
            f"({len(migration_files)} migration{'s' if len(migration_files) != 1 else ''})"
        ),
    )
    progress.remove_task(task)

    return MatrixBuildResult(
        name=matrix_config.name,
        output_dir=output_dir,
        matrix_uri=matrix_uri,
        total_triples=len(merged_graph),
        valid_files_count=len(validation_summary.valid_files),
        total_files_count=len(rdf_files),
        invalid_files=validation_summary.invalid_files,
        migration_files=tuple(p.name for p in migration_files),
    )


def execute_build_workflow(progress, config: PackageConfig) -> PackageBuildResult:
    """Execute the complete build workflow for a package.

    Args:
        progress: Rich progress instance for status updates
        config: Package configuration

    Returns:
        PackageBuildResult with package file and matrix results

    Raises:
        BuildError: If any matrix build fails
    """
    matrix_results: list[MatrixBuildResult] = []

    # Build each matrix
    for matrix_config in config.matrices:
        task = progress.add_task(
            f"Building matrix: {matrix_config.name}...",
            total=None,
        )
        progress.remove_task(task)

        result = build_matrix(progress, matrix_config, config.project_dir)
        matrix_results.append(result)

    # Create package archive
    task = progress.add_task("Creating package archive...", total=None)
    package_file = create_package_archive(
        config,
        matrix_results,
        config.project_dir / ".matrix" / "package.tgz",
    )
    progress.update(task, description=f"Package created: {package_file.name}")
    progress.remove_task(task)

    return PackageBuildResult(
        package_file=package_file,
        package_name=config.name,
        package_version=config.version,
        matrices=matrix_results,
    )


# Aliases for compatibility
create_package_config = create_build_config
build_package = execute_build_workflow


def display_build_results(result: PackageBuildResult, console: Console) -> None:
    """Display build results.

    Args:
        result: Build result to display
        console: Rich console for output
    """
    console.print(
        f"[green]Package built successfully: {result.package_name} v{result.package_version}[/green]"
    )
    console.print(f"[dim]  Package file: {result.package_file}[/dim]")
    console.print(f"[dim]  Matrices: {len(result.matrices)}[/dim]")
    for matrix in result.matrices:
        console.print(f"[dim]    - {matrix.name}: {matrix.total_triples} triples[/dim]")
