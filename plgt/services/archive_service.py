"""Archive utilities for creating deployment bundles."""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from plgt.services.rdf_operations import BuildError

if TYPE_CHECKING:
    from plgt.models.build_types import MatrixBuildResult, PackageConfig


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
        # Create parent directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Normalize to list
        if artifacts_dirs is None:
            dirs_list: list[Path] = []
        elif isinstance(artifacts_dirs, Path):
            dirs_list = [artifacts_dirs]
        else:
            dirs_list = artifacts_dirs

        migrations_list = list(migration_files or [])

        # Create tar.gz bundle
        with tarfile.open(output_path, "w:gz") as tar:
            # Add assembly.ttl from string content
            assembly_bytes = assembly_content.encode("utf-8")
            assembly_info = tarfile.TarInfo(name="assembly.ttl")
            assembly_info.size = len(assembly_bytes)
            tar.addfile(assembly_info, BytesIO(assembly_bytes))

            # Track added files to avoid duplicates
            added_files: set[str] = set()

            # Add migration files into migrations/ — flat layout keyed by filename.
            for migration_file in migrations_list:
                if not migration_file.is_file():
                    continue
                arcname = f"migrations/{migration_file.name}"
                if arcname in added_files:
                    continue
                tar.add(migration_file, arcname=arcname)
                added_files.add(arcname)

            # Add all artifact directories
            for artifacts_dir in dirs_list:
                if artifacts_dir and artifacts_dir.exists() and artifacts_dir.is_dir():
                    # Add files from this directory to artifacts/
                    for file_path in artifacts_dir.rglob("*"):
                        if file_path.is_file():
                            # Get relative path within the artifacts dir
                            rel_path = file_path.relative_to(artifacts_dir)
                            arcname = f"artifacts/{rel_path}"

                            # Skip if already added (from earlier directory)
                            if arcname in added_files:
                                continue

                            tar.add(file_path, arcname=arcname)
                            added_files.add(arcname)

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


def _add_root_file_if_present(
    tar: tarfile.TarFile, project_dir: Path, candidates: list[str]
) -> None:
    """Add the first matching root-level file (e.g. README.md, LICENSE) to the tarball.

    Tries each candidate name in order against ``project_dir`` and adds the first one that
    exists, preserving its canonical name. No-ops when none are present so the build still
    succeeds for packages that ship without these files.
    """
    for name in candidates:
        candidate = project_dir / name
        if candidate.is_file():
            data = candidate.read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
            return


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
        # Create parent directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)

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

        with tarfile.open(output_path, "w:gz") as tar:
            # Add manifest.json
            manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_bytes)
            tar.addfile(manifest_info, BytesIO(manifest_bytes))

            # Bundle README + LICENSE from the package source dir into the
            # tarball root. README at the tarball root is stored on the
            # version row for the registry detail page. LICENSE rides along for
            # compliance (Apache-2.0 §4(a) requires distributing a copy of
            # the License with any distribution); we don't render it in the
            # UI today, but the SPDX shorthand in manifest.json is the
            # display surface, and the bundled file ensures `plgt install`
            # produces a complete package on disk.
            _add_root_file_if_present(
                tar, package_config.project_dir, ["README.md", "readme.md"]
            )
            _add_root_file_if_present(
                tar,
                package_config.project_dir,
                ["LICENSE", "LICENSE.md", "LICENSE.txt", "license"],
            )

            # Add each matrix's contents
            for result in matrix_results:
                matrix_output_dir = result.output_dir

                # Add assembly.ttl from the matrix bundle
                assembly_path = matrix_output_dir / "matrix.tar.gz"
                if assembly_path.exists():
                    # Extract assembly.ttl from the matrix bundle and add it
                    with tarfile.open(assembly_path, "r:gz") as matrix_tar:
                        for member in matrix_tar.getmembers():
                            if member.name == "assembly.ttl":
                                # Read the file content
                                f = matrix_tar.extractfile(member)
                                if f:
                                    content = f.read()
                                    new_info = tarfile.TarInfo(
                                        name=f"{result.name}/assembly.ttl"
                                    )
                                    new_info.size = len(content)
                                    tar.addfile(new_info, BytesIO(content))
                            elif member.name.startswith(
                                "artifacts/"
                            ) or member.name.startswith("migrations/"):
                                # Copy artifacts and migrations with matrix prefix
                                f = matrix_tar.extractfile(member)
                                if f and member.isfile():
                                    content = f.read()
                                    new_info = tarfile.TarInfo(
                                        name=f"{result.name}/{member.name}"
                                    )
                                    new_info.size = len(content)
                                    tar.addfile(new_info, BytesIO(content))

        return output_path

    except Exception as e:
        msg = f"Failed to create package at {output_path}: {e}"
        raise BuildError(msg) from e
