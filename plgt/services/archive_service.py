"""Archive utilities for creating deployment bundles."""

from __future__ import annotations

import gzip
import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from plgt.services.rdf_operations import BuildError

if TYPE_CHECKING:
    from plgt.models.build_types import MatrixBuildResult, PackageConfig


def _deterministic_tarinfo(name: str, size: int) -> tarfile.TarInfo:
    """Build a ``TarInfo`` whose metadata is fully fixed.

    All environment-derived fields (mtime, uid/gid, owner names, mode) are
    zeroed/normalized so the same content always serializes to the same header
    bytes regardless of when or where the build runs.
    """
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    return info


def _write_reproducible_tar_gz(
    output_path: Path, members: list[tuple[str, bytes]]
) -> None:
    """Write a reproducible ``.tar.gz``.

    Members are emitted in sorted name order with fixed metadata, and the gzip
    header carries neither a timestamp nor an original filename. Identical inputs
    therefore always produce byte-identical archives, so a content hash of the
    archive is stable across machines and rebuilds. Duplicate names keep their
    first occurrence (matching the order in which callers append them).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    ordered: list[tuple[str, bytes]] = []
    for name, data in members:
        if name in seen:
            continue
        seen.add(name)
        ordered.append((name, data))
    ordered.sort(key=lambda member: member[0])

    with output_path.open("wb") as raw:
        # mtime=0 + empty filename keep the gzip header free of wall-clock data,
        # which is otherwise the single largest source of build-to-build drift.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for name, data in ordered:
                    tar.addfile(_deterministic_tarinfo(name, len(data)), BytesIO(data))


def create_tar_gz_bundle(
    assembly_content: str,
    artifacts_dirs: list[Path] | Path | None,
    output_path: Path,
    migration_files: list[Path] | None = None,
) -> Path:
    """Create a tar.gz bundle containing assembly.ttl and optional artifacts/ + migrations/.

    Args:
        assembly_content: The merged RDF content as Turtle string
        artifacts_dirs: List of paths to artifact directories to include (or single Path)
        output_path: Path for the output bundle (e.g., .matrix/matrix.tar.gz)
        migration_files: List of paths to ``migrations/from-*.rq`` files to include under the
            bundle's ``migrations/`` directory. Each file is added at its base filename so the
            server-side loader sees a flat ``migrations/from-<version>.rq`` layout regardless
            of where the sources live in the package tree.

    Returns:
        Path to the created bundle

    Raises:
        BuildError: If bundle creation fails
    """
    try:
        # Normalize to list
        if artifacts_dirs is None:
            dirs_list: list[Path] = []
        elif isinstance(artifacts_dirs, Path):
            dirs_list = [artifacts_dirs]
        else:
            dirs_list = artifacts_dirs

        migrations_list = list(migration_files or [])

        # Collect (arcname, content) members; the reproducible writer reads the
        # file bytes itself (never `tar.add`) so no on-disk metadata leaks in.
        members: list[tuple[str, bytes]] = [
            ("assembly.ttl", assembly_content.encode("utf-8")),
        ]

        # Migration files into migrations/ — flat layout keyed by filename.
        for migration_file in migrations_list:
            if migration_file.is_file():
                members.append(
                    (f"migrations/{migration_file.name}", migration_file.read_bytes())
                )

        # Artifact directories into artifacts/. Sorted so the member order is
        # independent of filesystem traversal order.
        for artifacts_dir in dirs_list:
            if artifacts_dir and artifacts_dir.exists() and artifacts_dir.is_dir():
                for file_path in sorted(artifacts_dir.rglob("*")):
                    if file_path.is_file():
                        rel_path = file_path.relative_to(artifacts_dir).as_posix()
                        members.append(
                            (f"artifacts/{rel_path}", file_path.read_bytes())
                        )

        _write_reproducible_tar_gz(output_path, members)
        return output_path

    except Exception as e:
        msg = f"Failed to create bundle at {output_path}: {e}"
        raise BuildError(msg) from e


def discover_artifacts_directory(spec_dir: Path) -> Path | None:
    """Discover artifacts directory in spec directory if it exists.

    Args:
        spec_dir: Path to the specification directory

    Returns:
        Path to artifacts directory, or None if not found or empty
    """
    artifacts_dir = spec_dir / "artifacts"

    if not artifacts_dir.exists():
        return None

    if not artifacts_dir.is_dir():
        return None

    # Check if directory has any files (not just empty directories)
    has_files = any(artifacts_dir.rglob("*"))
    if not has_files:
        return None

    return artifacts_dir


def _first_present_root_file(
    project_dir: Path, candidates: list[str]
) -> tuple[str, bytes] | None:
    """Return ``(name, bytes)`` for the first matching root-level file, or None.

    Tries each candidate name in order against ``project_dir`` and returns the
    first that exists, preserving its canonical name. Returns None when none are
    present so the build still succeeds for packages that ship without them.
    """
    for name in candidates:
        candidate = project_dir / name
        if candidate.is_file():
            return (name, candidate.read_bytes())
    return None


def create_package_archive(
    package_config: PackageConfig,
    matrix_results: list[MatrixBuildResult],
    output_path: Path,
) -> Path:
    """Create a package.tgz containing manifest.json and all matrix bundles.

    The package structure is:
        package.tgz/
        ├── manifest.json
        ├── {matrix_name}/
        │   ├── assembly.ttl
        │   └── artifacts/
        └── {matrix_name}/
            ├── assembly.ttl
            └── artifacts/

    Args:
        package_config: Package configuration with name and version
        matrix_results: List of built matrix results
        output_path: Path for the output package (e.g., .matrix/package.tgz)

    Returns:
        Path to the created package

    Raises:
        BuildError: If package creation fails
    """
    try:
        # Build manifest
        manifest: dict = {
            "name": package_config.name,
            "version": package_config.version,
            "engineVersion": package_config.engine_version,
            # Path is the in-tarball relative path to the matrix's assembly.ttl. Server-side
            # discovery and install both look up a regular-file member by exact path, not
            # a directory; emitting the bare directory name used to break both paths with
            # "manifest.json references X but the tarball does not contain it".
            "matrices": [
                {
                    "name": result.name,
                    "path": f"{result.name}/assembly.ttl",
                    "uri": result.matrix_uri,
                }
                for result in matrix_results
            ],
        }
        # Thread the optional package-level metadata fields. Only emit when non-empty so a
        # package without these fields keeps a minimal manifest shape and the server-side
        # parser sees absent keys (not blanks).
        if package_config.description:
            manifest["description"] = package_config.description
        if package_config.repository_url:
            manifest["repositoryUrl"] = package_config.repository_url
        if package_config.homepage:
            manifest["homepage"] = package_config.homepage
        if package_config.license:
            manifest["license"] = package_config.license
        if package_config.changelog:
            manifest["changelog"] = package_config.changelog
        if package_config.tags:
            manifest["tags"] = list(package_config.tags)
        # If the package declared cross-package deps in ``poliglot.yml``, persist them into the
        # in-archive manifest as a list-of-objects so the registry can store them at publish
        # time and the platform's resolver can read them at install time without re-parsing
        # the YAML. Only emit the field when there are declared deps so packages without
        # dependencies keep a minimal manifest shape.
        if package_config.dependencies:
            manifest["dependencies"] = [
                {
                    "publisher": dep.publisher,
                    "name": dep.name,
                    "versionRange": dep.version_range,
                }
                for dep in package_config.dependencies
            ]

        members: list[tuple[str, bytes]] = [
            ("manifest.json", json.dumps(manifest, indent=2).encode("utf-8")),
        ]

        # Bundle README + LICENSE from the package source dir into the tarball
        # root. README at the tarball root is stored on the version row for the
        # registry detail page. LICENSE rides along for compliance (Apache-2.0
        # §4(a) requires distributing a copy of the License with any
        # distribution); we don't render it in the UI today, but the SPDX
        # shorthand in manifest.json is the display surface, and the bundled file
        # ensures `plgt install` produces a complete package on disk.
        readme = _first_present_root_file(
            package_config.project_dir, ["README.md", "readme.md"]
        )
        if readme:
            members.append(readme)
        license_file = _first_present_root_file(
            package_config.project_dir,
            ["LICENSE", "LICENSE.md", "LICENSE.txt", "license"],
        )
        if license_file:
            members.append(license_file)

        # Add each matrix's contents, re-keyed under the matrix name.
        for result in matrix_results:
            assembly_path = result.output_dir / "matrix.tar.gz"
            if not assembly_path.exists():
                continue
            with tarfile.open(assembly_path, "r:gz") as matrix_tar:
                for member in matrix_tar.getmembers():
                    if member.name == "assembly.ttl":
                        f = matrix_tar.extractfile(member)
                        if f:
                            members.append((f"{result.name}/assembly.ttl", f.read()))
                    elif member.isfile() and (
                        member.name.startswith("artifacts/")
                        or member.name.startswith("migrations/")
                    ):
                        f = matrix_tar.extractfile(member)
                        if f:
                            members.append((f"{result.name}/{member.name}", f.read()))

        _write_reproducible_tar_gz(output_path, members)
        return output_path

    except Exception as e:
        msg = f"Failed to create package at {output_path}: {e}"
        raise BuildError(msg) from e
