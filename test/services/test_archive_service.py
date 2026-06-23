"""Unit tests for archive service."""

import os
import struct
import tarfile
from pathlib import Path

from plgt.models.build_types import PackageConfig
from plgt.services.archive_service import (
    create_package_archive,
    create_tar_gz_bundle,
    discover_artifacts_directory,
)


class TestCreateTarGzBundle:
    """Tests for create_tar_gz_bundle function."""

    def test_create_bundle_no_artifacts(self, tmp_path):
        """Test creating bundle with only assembly.ttl."""
        assembly_content = "@prefix test: <http://test.com#> ."
        output_path = tmp_path / "bundle.tar.gz"

        result = create_tar_gz_bundle(assembly_content, None, output_path)

        assert result == output_path
        assert output_path.exists()

        # Verify bundle contents
        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "assembly.ttl" in names
            assert len(names) == 1

    def test_create_bundle_with_artifacts(self, tmp_path):
        """Test creating bundle with artifacts directory."""
        assembly_content = "@prefix test: <http://test.com#> ."

        # Create artifacts directory
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "test.md").write_text("# Test")
        (artifacts_dir / "config.json").write_text('{"key": "value"}')

        output_path = tmp_path / "bundle.tar.gz"

        result = create_tar_gz_bundle(assembly_content, artifacts_dir, output_path)

        assert result == output_path
        assert output_path.exists()

        # Verify bundle contents
        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "assembly.ttl" in names
            assert "artifacts/test.md" in names or "artifacts\\test.md" in names
            assert "artifacts/config.json" in names or "artifacts\\config.json" in names

    def test_create_bundle_empty_artifacts_directory(self, tmp_path):
        """Test that empty artifacts directory is not included."""
        assembly_content = "@prefix test: <http://test.com#> ."

        # Create empty artifacts directory
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        output_path = tmp_path / "bundle.tar.gz"

        result = create_tar_gz_bundle(assembly_content, artifacts_dir, output_path)

        assert result == output_path

        # Empty directory should not be included
        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "assembly.ttl" in names
            # Should only have assembly.ttl, no artifacts
            assert len(names) == 1


class TestDiscoverArtifactsDirectory:
    """Tests for discover_artifacts_directory function."""

    def test_discover_existing_directory_with_files(self, tmp_path):
        """Test discovering artifacts directory that exists and has files."""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "test.md").write_text("content")

        result = discover_artifacts_directory(tmp_path)

        assert result == artifacts_dir

    def test_discover_missing_directory(self, tmp_path):
        """Test discovering when artifacts directory doesn't exist."""
        result = discover_artifacts_directory(tmp_path)

        assert result is None

    def test_discover_empty_directory(self, tmp_path):
        """Test discovering empty artifacts directory returns None."""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        result = discover_artifacts_directory(tmp_path)

        assert result is None

    def test_discover_directory_is_file(self, tmp_path):
        """Test when 'artifacts' exists but is a file, not directory."""
        artifacts_file = tmp_path / "artifacts"
        artifacts_file.write_text("not a directory")

        result = discover_artifacts_directory(tmp_path)

        assert result is None


class TestCreatePackageArchive:
    """Tests for create_package_archive — specifically README / LICENSE bundling.

    The platform reads README.md from the tarball root and stores it on the version row
    for the registry detail page. LICENSE rides along for compliance with Apache-2.0 §4(a).
    Both must land at the tarball root with their canonical filenames; missing files don't
    fail the build.
    """

    def _config(self, project_dir: Path) -> PackageConfig:
        return PackageConfig(
            name="demo",
            version="1.0.0",
            engine_version=">=1 <2",
            project_dir=project_dir,
        )

    def test_bundles_readme_and_license_at_tarball_root(self, tmp_path: Path):
        project_dir = tmp_path / "pkg"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("# demo\n\nUseful matrix.\n")
        (project_dir / "LICENSE").write_text("Apache License 2.0\n")
        output_path = tmp_path / "demo-package.tgz"

        create_package_archive(self._config(project_dir), [], output_path)

        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names
            assert "README.md" in names
            assert "LICENSE" in names
            readme = tar.extractfile("README.md")
            assert readme is not None
            assert readme.read().decode() == "# demo\n\nUseful matrix.\n"

    def test_picks_uppercase_readme_when_both_present(self, tmp_path: Path):
        project_dir = tmp_path / "pkg"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("upper")
        (project_dir / "readme.md").write_text("lower")
        output_path = tmp_path / "demo-package.tgz"

        create_package_archive(self._config(project_dir), [], output_path)

        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "README.md" in names
            # We only emit ONE README entry — the canonical capitalised form wins.
            assert "readme.md" not in names

    def test_omits_missing_root_files(self, tmp_path: Path):
        project_dir = tmp_path / "pkg"
        project_dir.mkdir()
        output_path = tmp_path / "demo-package.tgz"

        create_package_archive(self._config(project_dir), [], output_path)

        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names
            assert "README.md" not in names
            assert "LICENSE" not in names

    def test_accepts_license_variants(self, tmp_path: Path):
        project_dir = tmp_path / "pkg"
        project_dir.mkdir()
        (project_dir / "LICENSE.md").write_text("md-license")
        output_path = tmp_path / "demo-package.tgz"

        create_package_archive(self._config(project_dir), [], output_path)

        with tarfile.open(output_path, "r:gz") as tar:
            names = tar.getnames()
            assert "LICENSE.md" in names


def _gzip_header_mtime(path: Path) -> int:
    """Read the MTIME field (bytes 4..8, little-endian) from a gzip header."""
    header = path.read_bytes()[:10]
    assert header[:2] == b"\x1f\x8b", "not a gzip stream"
    return struct.unpack("<I", header[4:8])[0]


class TestReproducibility:
    """The archives must be byte-identical for identical content, so a content
    hash of the tarball is stable across machines and rebuilds."""

    def test_bundle_is_byte_identical_across_rebuilds(self, tmp_path: Path):
        assembly = "@prefix t: <http://t#> .\nt:a t:b t:c .\n"
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "components.js").write_text("module.exports={};")
        (artifacts / "queries").mkdir()
        (artifacts / "queries" / "get.graphql").write_text("query{ id }")

        first = tmp_path / "first.tar.gz"
        second = tmp_path / "second.tar.gz"

        create_tar_gz_bundle(assembly, artifacts, first)
        # Touch the source files to a different mtime — a reproducible build must
        # NOT pick up on-disk timestamps.
        for f in artifacts.rglob("*"):
            os.utime(f, (1_000_000, 1_000_000))
        create_tar_gz_bundle(assembly, artifacts, second)

        assert first.read_bytes() == second.read_bytes()

    def test_bundle_gzip_header_has_no_timestamp(self, tmp_path: Path):
        out = tmp_path / "b.tar.gz"
        create_tar_gz_bundle("@prefix t: <http://t#> .", None, out)
        assert _gzip_header_mtime(out) == 0

    def test_bundle_members_are_sorted(self, tmp_path: Path):
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        for name in ["zebra.txt", "alpha.txt", "mango.txt"]:
            (artifacts / name).write_text(name)
        out = tmp_path / "b.tar.gz"

        create_tar_gz_bundle("@prefix t: <http://t#> .", artifacts, out)

        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        assert names == sorted(names)

    def test_package_archive_is_byte_identical_across_rebuilds(self, tmp_path: Path):
        project_dir = tmp_path / "pkg"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("# demo\n")
        (project_dir / "LICENSE").write_text("Apache-2.0\n")
        config = PackageConfig(
            name="demo",
            version="1.0.0",
            engine_version=">=1 <2",
            project_dir=project_dir,
        )

        first = tmp_path / "first.tgz"
        second = tmp_path / "second.tgz"
        create_package_archive(config, [], first)
        os.utime(project_dir / "README.md", (1_000_000, 1_000_000))
        create_package_archive(config, [], second)

        assert first.read_bytes() == second.read_bytes()
        assert _gzip_header_mtime(first) == 0
