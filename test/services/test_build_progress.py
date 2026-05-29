"""Unit tests for build_progress module.

Tests cover build progress tracking and validation functions.
"""

import tempfile
from pathlib import Path

from plgt.services.build_progress import (
    create_progress_tracker,
    display_build_success,
    display_validation_warnings,
    validate_files_with_progress,
)
from rich.console import Console
from rich.progress import Progress


class TestCreateProgressTracker:
    """Test progress tracker creation."""

    def test_create_progress_tracker(self):
        """Test creating a progress tracker with console."""
        console = Console()
        progress = create_progress_tracker(console)

        assert progress is not None
        assert isinstance(progress, Progress)


class TestValidateFilesWithProgress:
    """Test file validation with progress tracking."""

    def test_validate_all_valid_files(self):
        """Test validation when all files are valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.ttl"
            file2 = Path(tmpdir) / "test2.ttl"
            file1.write_text("@prefix ex: <http://example.org/> .")
            file2.write_text("@prefix ex: <http://example.org/> .")

            console = Console()
            progress = create_progress_tracker(console)

            def mock_validate(path):
                return True, None

            with progress:
                result = validate_files_with_progress(
                    progress, [file1, file2], mock_validate
                )

            assert len(result.valid_files) == 2
            assert len(result.invalid_files) == 0

    def test_validate_mixed_valid_invalid(self):
        """Test validation with mix of valid and invalid files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            valid_file = Path(tmpdir) / "valid.ttl"
            invalid_file = Path(tmpdir) / "invalid.ttl"
            valid_file.write_text("@prefix ex: <http://example.org/> .")
            invalid_file.write_text("invalid")

            console = Console()
            progress = create_progress_tracker(console)

            def mock_validate(path):
                if "invalid" in path.name:
                    return False, "Invalid RDF syntax"
                return True, None

            with progress:
                result = validate_files_with_progress(
                    progress, [valid_file, invalid_file], mock_validate
                )

            assert len(result.valid_files) == 1
            assert len(result.invalid_files) == 1
            assert result.invalid_files[0][0] == invalid_file
            assert "Invalid RDF syntax" in result.invalid_files[0][1]

    def test_validate_all_invalid_files(self):
        """Test validation when all files are invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "bad1.ttl"
            file2 = Path(tmpdir) / "bad2.ttl"
            file1.write_text("invalid")
            file2.write_text("also invalid")

            console = Console()
            progress = create_progress_tracker(console)

            def mock_validate(path):
                return False, f"Error in {path.name}"

            with progress:
                result = validate_files_with_progress(
                    progress, [file1, file2], mock_validate
                )

            assert len(result.valid_files) == 0
            assert len(result.invalid_files) == 2

    def test_validate_empty_list(self):
        """Test validation with empty file list."""
        console = Console()
        progress = create_progress_tracker(console)

        def mock_validate(path):
            return True, None

        with progress:
            result = validate_files_with_progress(progress, [], mock_validate)

        assert len(result.valid_files) == 0
        assert len(result.invalid_files) == 0

    def test_progress_updates_correctly(self):
        """Test that progress is updated for each file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = [Path(tmpdir) / f"test{i}.ttl" for i in range(5)]
            for f in files:
                f.write_text("@prefix ex: <http://example.org/> .")

            console = Console()
            progress = create_progress_tracker(console)

            call_count = {"count": 0}

            def counting_validate(path):
                call_count["count"] += 1
                return True, None

            with progress:
                validate_files_with_progress(progress, files, counting_validate)

            # Validate function should be called for each file
            assert call_count["count"] == 5


class TestDisplayFunctions:
    """Test display helper functions."""

    def test_display_validation_warnings_with_errors(self):
        """Test displaying validation warnings."""
        console = Console()
        invalid_files = [
            (Path("/test/file1.ttl"), "Syntax error"),
            (Path("/test/file2.ttl"), "Parse error"),
        ]

        # Should not raise errors
        display_validation_warnings(console, invalid_files)

    def test_display_validation_warnings_empty(self):
        """Test displaying warnings with no invalid files."""
        console = Console()
        invalid_files = []

        # Should not display anything or raise errors
        display_validation_warnings(console, invalid_files)

    def test_display_build_success(self):
        """Test displaying build success message."""
        console = Console()

        # Should not raise errors
        display_build_success(console)
