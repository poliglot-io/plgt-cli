"""Artifact management service for build artifact discovery and freshness checks.

This module provides functions for discovering build artifacts, checking their
freshness against source specifications, and determining when auto-builds are needed.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from plgt.core import settings
from plgt.core.exceptions import CLIError

logger = logging.getLogger(settings.APP_AUTHOR)


class ArtifactError(CLIError):
    """Raised when artifact operations encounter an error."""


class ArtifactInfo(NamedTuple):
    """Information about a build artifact."""

    path: Path
    exists: bool
    size_bytes: int | None = None
    modified_time: datetime | None = None


class FreshnessCheck(NamedTuple):
    """Result of artifact freshness check."""

    is_fresh: bool
    artifact_info: ArtifactInfo
    spec_modified_time: datetime | None
    reason: str


def get_artifact_path(project_root: Path | None = None) -> Path:
    """Get the path to the build artifact file.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        Path to the compressed turtle artifact file
    """
    if project_root is None:
        project_root = Path.cwd()

    return project_root / ".matrix" / "compressed.ttl.gz"


def get_spec_directory(project_root: Path | None = None) -> Path:
    """Get the path to the specification directory.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        Path to the spec directory
    """
    if project_root is None:
        project_root = Path.cwd()

    return project_root / "spec"


def discover_artifact(project_root: Path | None = None) -> ArtifactInfo:
    """Discover build artifact and return its information.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        ArtifactInfo containing artifact details

    Raises:
        ArtifactError: If artifact discovery encounters an error
    """
    try:
        artifact_path = get_artifact_path(project_root)

        if not artifact_path.exists():
            return ArtifactInfo(
                path=artifact_path,
                exists=False,
            )

        # Get file statistics
        stat = artifact_path.stat()
        modified_time = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

        logger.debug(
            "Found artifact at: %s, size: %s, modified: %s",
            artifact_path,
            stat.st_size,
            modified_time,
        )

        return ArtifactInfo(
            path=artifact_path,
            exists=True,
            size_bytes=stat.st_size,
            modified_time=modified_time,
        )

    except Exception as e:
        msg = f"Failed to discover artifact: {e}"
        raise ArtifactError(msg) from e


def get_spec_last_modified(spec_dir: Path) -> datetime | None:
    """Get the last modification time of any file in the spec directory.

    Args:
        spec_dir: Path to the specification directory

    Returns:
        Last modification time of any file in spec directory, None if directory doesn't exist

    Raises:
        ArtifactError: If spec directory analysis fails
    """
    try:
        if not spec_dir.exists():
            return None

        if not spec_dir.is_dir():
            return None

        # Find the most recent modification time of any file in spec directory
        # Only scan the immediate spec directory, not subdirectories
        latest_time = None

        # Check modification time of the spec directory itself
        dir_stat = spec_dir.stat()
        latest_time = datetime.fromtimestamp(dir_stat.st_mtime, tz=UTC)

        # Check all files in the immediate spec directory only (not recursive)
        for item in spec_dir.iterdir():
            try:
                if item.is_file():
                    file_stat = item.stat()
                    file_time = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
                    latest_time = max(latest_time, file_time)
                elif item.is_dir():
                    # Check subdirectories but don't recurse into them
                    dir_stat = item.stat()
                    dir_time = datetime.fromtimestamp(dir_stat.st_mtime, tz=UTC)
                    latest_time = max(latest_time, dir_time)
            except (OSError, PermissionError) as e:
                # Log warning but continue
                logger.warning("Could not stat item %s: %s", item, e)
                continue

        return latest_time

    except Exception as e:
        msg = f"Failed to analyze spec directory modification times: {e}"
        raise ArtifactError(msg) from e


def check_artifact_freshness(project_root: Path | None = None) -> FreshnessCheck:
    """Check if build artifact is fresh compared to specification files.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        FreshnessCheck result indicating if artifact needs rebuilding

    Raises:
        ArtifactError: If freshness check encounters an error
    """
    try:
        artifact_info = discover_artifact(project_root)
        spec_dir = get_spec_directory(project_root)
        spec_modified_time = get_spec_last_modified(spec_dir)

        # If artifact doesn't exist, it's not fresh
        if not artifact_info.exists:
            return FreshnessCheck(
                is_fresh=False,
                artifact_info=artifact_info,
                spec_modified_time=spec_modified_time,
                reason="Artifact does not exist",
            )

        # If we can't determine spec modification time, consider artifact stale
        if spec_modified_time is None:
            return FreshnessCheck(
                is_fresh=False,
                artifact_info=artifact_info,
                spec_modified_time=spec_modified_time,
                reason="Cannot determine spec directory modification time",
            )

        # If artifact doesn't have modification time, consider it stale
        if artifact_info.modified_time is None:
            return FreshnessCheck(
                is_fresh=False,
                artifact_info=artifact_info,
                spec_modified_time=spec_modified_time,
                reason="Cannot determine artifact modification time",
            )

        # Compare modification times
        if spec_modified_time > artifact_info.modified_time:
            return FreshnessCheck(
                is_fresh=False,
                artifact_info=artifact_info,
                spec_modified_time=spec_modified_time,
                reason=f"Spec files modified after artifact (spec: {spec_modified_time}, artifact: {artifact_info.modified_time})",
            )

        # Artifact is fresh
        return FreshnessCheck(
            is_fresh=True,
            artifact_info=artifact_info,
            spec_modified_time=spec_modified_time,
            reason="Artifact is up to date",
        )

    except Exception as e:
        msg = f"Failed to check artifact freshness: {e}"
        raise ArtifactError(msg) from e


def needs_rebuild(project_root: Path | None = None) -> bool:
    """Check if project needs a rebuild based on artifact freshness.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        True if rebuild is needed, False otherwise

    Raises:
        ArtifactError: If rebuild check encounters an error
    """
    freshness_check = check_artifact_freshness(project_root)
    return not freshness_check.is_fresh


def validate_artifact_for_install(project_root: Path | None = None) -> ArtifactInfo:
    """Validate that artifact exists and is ready for installation.

    Args:
        project_root: Optional project root path. If not provided, uses current directory.

    Returns:
        ArtifactInfo for the validated artifact

    Raises:
        ArtifactError: If artifact is not ready for installation
    """
    artifact_info = discover_artifact(project_root)

    if not artifact_info.exists:
        msg = "Build artifact does not exist. Run 'plgt build' first."
        raise ArtifactError(msg)

    if artifact_info.size_bytes is None or artifact_info.size_bytes == 0:
        msg = "Build artifact is empty or corrupted. Run 'plgt build' to recreate."
        raise ArtifactError(msg)

    logger.info(
        "Artifact validated for installation: %s (%s bytes)",
        artifact_info.path,
        artifact_info.size_bytes,
    )
    return artifact_info
