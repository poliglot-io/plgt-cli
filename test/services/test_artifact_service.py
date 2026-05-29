"""Unit tests for artifact_service module.

Tests cover artifact discovery, freshness checking, and validation operations.
"""

import gzip
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from plgt.services.artifact_service import (
    ArtifactError,
    check_artifact_freshness,
    discover_artifact,
    get_artifact_path,
    get_spec_directory,
    get_spec_last_modified,
    needs_rebuild,
    validate_artifact_for_install,
)


class TestArtifactPaths:
    """Test path resolution functions."""

    def test_get_artifact_path_default(self):
        """Test getting artifact path with default project root."""
        artifact_path = get_artifact_path()
        assert artifact_path == Path.cwd() / ".matrix" / "compressed.ttl.gz"

    def test_get_artifact_path_custom_root(self):
        """Test getting artifact path with custom project root."""
        custom_root = Path("/custom/project")
        artifact_path = get_artifact_path(custom_root)
        assert artifact_path == custom_root / ".matrix" / "compressed.ttl.gz"

    def test_get_spec_directory_default(self):
        """Test getting spec directory with default project root."""
        spec_dir = get_spec_directory()
        assert spec_dir == Path.cwd() / "spec"

    def test_get_spec_directory_custom_root(self):
        """Test getting spec directory with custom project root."""
        custom_root = Path("/custom/project")
        spec_dir = get_spec_directory(custom_root)
        assert spec_dir == custom_root / "spec"


class TestArtifactDiscovery:
    """Test artifact discovery functionality."""

    def test_discover_artifact_nonexistent(self):
        """Test discovering non-existent artifact."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            artifact_info = discover_artifact(temp_path)

            assert not artifact_info.exists
            assert artifact_info.path == temp_path / ".matrix" / "compressed.ttl.gz"
            assert artifact_info.size_bytes is None
            assert artifact_info.modified_time is None

    def test_discover_artifact_exists(self):
        """Test discovering existing artifact."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create artifact file
            artifact_path = matrix_dir / "compressed.ttl.gz"
            test_content = (
                "@prefix ex: <http://example.org/> .\nex:test ex:predicate ex:value .\n"
            )

            with gzip.open(artifact_path, "wt", encoding="utf-8") as f:
                f.write(test_content)

            artifact_info = discover_artifact(temp_path)

            assert artifact_info.exists
            assert artifact_info.path == artifact_path
            assert artifact_info.size_bytes > 0
            assert artifact_info.modified_time is not None
            assert isinstance(artifact_info.modified_time, datetime)

    def test_discover_artifact_error_handling(self):
        """Test error handling in artifact discovery."""
        # Test with invalid path that would cause permission error
        with patch("pathlib.Path.stat", side_effect=PermissionError("Access denied")):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                matrix_dir = temp_path / ".matrix"
                matrix_dir.mkdir()
                artifact_path = matrix_dir / "compressed.ttl.gz"
                artifact_path.write_text("dummy")

                with pytest.raises(ArtifactError, match="Failed to discover artifact"):
                    discover_artifact(temp_path)


class TestSpecModificationTime:
    """Test spec directory modification time analysis."""

    def test_get_spec_last_modified_nonexistent_dir(self):
        """Test spec modification time for non-existent directory."""
        nonexistent_dir = Path("/absolutely/nonexistent/path")
        result = get_spec_last_modified(nonexistent_dir)
        assert result is None

    def test_get_spec_last_modified_not_dir(self):
        """Test spec modification time when path is not a directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            file_path = temp_path / "not_a_dir.txt"
            file_path.write_text("test")

            result = get_spec_last_modified(file_path)
            assert result is None

    def test_get_spec_last_modified_empty_dir(self):
        """Test spec modification time for empty directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir) / "spec"
            spec_dir.mkdir()

            result = get_spec_last_modified(spec_dir)
            assert result is not None
            assert isinstance(result, datetime)

    def test_get_spec_last_modified_with_files(self):
        """Test spec modification time with files in directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir) / "spec"
            spec_dir.mkdir()

            # Create files with different modification times
            file1 = spec_dir / "file1.ttl"
            file1.write_text("content1")

            # Sleep to ensure different timestamps
            time.sleep(0.1)

            file2 = spec_dir / "file2.ttl"
            file2.write_text("content2")

            result = get_spec_last_modified(spec_dir)
            assert result is not None

            # Should return the latest modification time
            file2_mtime = datetime.fromtimestamp(file2.stat().st_mtime, tz=UTC)
            assert (
                abs((result - file2_mtime).total_seconds()) < 2.0
            )  # Allow some tolerance

    def test_get_spec_last_modified_with_subdirs(self):
        """Test spec modification time with subdirectories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            spec_dir = Path(temp_dir) / "spec"
            spec_dir.mkdir()

            # Create subdirectory with file
            sub_dir = spec_dir / "modules"
            sub_dir.mkdir()

            time.sleep(0.1)

            sub_file = sub_dir / "module.ttl"
            sub_file.write_text("module content")

            result = get_spec_last_modified(spec_dir)
            assert result is not None

            # Should find the file in subdirectory
            sub_file_mtime = datetime.fromtimestamp(sub_file.stat().st_mtime, tz=UTC)
            assert abs((result - sub_file_mtime).total_seconds()) < 2.0

    def test_get_spec_last_modified_error_handling(self):
        """Test error handling in spec modification time analysis."""
        with patch("pathlib.Path.iterdir", side_effect=OSError("Iterdir failed")):
            with tempfile.TemporaryDirectory() as temp_dir:
                spec_dir = Path(temp_dir) / "spec"
                spec_dir.mkdir()

                with pytest.raises(
                    ArtifactError, match="Failed to analyze spec directory"
                ):
                    get_spec_last_modified(spec_dir)


class TestArtifactFreshness:
    """Test artifact freshness checking."""

    def test_check_artifact_freshness_no_artifact(self):
        """Test freshness check when artifact doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            spec_dir = temp_path / "spec"
            spec_dir.mkdir()

            freshness_check = check_artifact_freshness(temp_path)

            assert not freshness_check.is_fresh
            assert not freshness_check.artifact_info.exists
            assert "does not exist" in freshness_check.reason

    def test_check_artifact_freshness_no_spec(self):
        """Test freshness check when spec directory doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create artifact
            artifact_path = matrix_dir / "compressed.ttl.gz"
            with gzip.open(artifact_path, "wt") as f:
                f.write("@prefix ex: <http://example.org/> .\n")

            freshness_check = check_artifact_freshness(temp_path)

            assert not freshness_check.is_fresh
            assert freshness_check.artifact_info.exists
            assert (
                "Cannot determine spec directory modification time"
                in freshness_check.reason
            )

    def test_check_artifact_freshness_fresh(self):
        """Test freshness check when artifact is fresh."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            spec_dir = temp_path / "spec"
            spec_dir.mkdir()
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create spec file first
            spec_file = spec_dir / "test.ttl"
            spec_file.write_text("@prefix ex: <http://example.org/> .\n")

            # Sleep to ensure different timestamps
            time.sleep(0.1)

            # Create artifact after spec file
            artifact_path = matrix_dir / "compressed.ttl.gz"
            with gzip.open(artifact_path, "wt") as f:
                f.write("@prefix ex: <http://example.org/> .\n")

            freshness_check = check_artifact_freshness(temp_path)

            assert freshness_check.is_fresh
            assert freshness_check.artifact_info.exists
            assert "up to date" in freshness_check.reason

    def test_check_artifact_freshness_stale(self):
        """Test freshness check when artifact is stale."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            spec_dir = temp_path / "spec"
            spec_dir.mkdir()
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create artifact first
            artifact_path = matrix_dir / "compressed.ttl.gz"
            with gzip.open(artifact_path, "wt") as f:
                f.write("@prefix ex: <http://example.org/> .\n")

            # Sleep to ensure different timestamps
            time.sleep(0.1)

            # Create spec file after artifact
            spec_file = spec_dir / "test.ttl"
            spec_file.write_text("@prefix ex: <http://example.org/> .\n")

            freshness_check = check_artifact_freshness(temp_path)

            assert not freshness_check.is_fresh
            assert freshness_check.artifact_info.exists
            assert "Spec files modified after artifact" in freshness_check.reason


class TestNeedsRebuild:
    """Test rebuild necessity checking."""

    def test_needs_rebuild_missing_artifact(self):
        """Test rebuild needed when artifact is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            spec_dir = temp_path / "spec"
            spec_dir.mkdir()

            assert needs_rebuild(temp_path) is True

    def test_needs_rebuild_fresh_artifact(self):
        """Test rebuild not needed when artifact is fresh."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            spec_dir = temp_path / "spec"
            spec_dir.mkdir()
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create spec file first
            spec_file = spec_dir / "test.ttl"
            spec_file.write_text("content")

            time.sleep(0.1)

            # Create fresh artifact
            artifact_path = matrix_dir / "compressed.ttl.gz"
            with gzip.open(artifact_path, "wt") as f:
                f.write("@prefix ex: <http://example.org/> .\n")

            assert needs_rebuild(temp_path) is False


class TestValidateArtifactForDeploy:
    """Test deployment validation."""

    def test_validate_artifact_for_install_missing(self):
        """Test validation fails when artifact is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            with pytest.raises(ArtifactError, match="Build artifact does not exist"):
                validate_artifact_for_install(temp_path)

    def test_validate_artifact_for_install_empty(self):
        """Test validation fails when artifact is empty."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create empty artifact
            artifact_path = matrix_dir / "compressed.ttl.gz"
            artifact_path.write_bytes(b"")

            with pytest.raises(ArtifactError, match="empty or corrupted"):
                validate_artifact_for_install(temp_path)

    def test_validate_artifact_for_install_valid(self):
        """Test validation succeeds for valid artifact."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_dir = temp_path / ".matrix"
            matrix_dir.mkdir()

            # Create valid artifact
            artifact_path = matrix_dir / "compressed.ttl.gz"
            with gzip.open(artifact_path, "wt") as f:
                f.write(
                    "@prefix ex: <http://example.org/> .\nex:test ex:predicate ex:value .\n"
                )

            artifact_info = validate_artifact_for_install(temp_path)

            assert artifact_info.exists
            assert artifact_info.size_bytes > 0
            assert artifact_info.path == artifact_path
