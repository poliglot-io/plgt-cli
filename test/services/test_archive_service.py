"""Unit tests for archive service."""

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
