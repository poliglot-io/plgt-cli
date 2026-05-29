"""Data types for lifecycle command operations.

This module contains types for the package-based lifecycle command system,
replacing the older release-based types for package commands.
"""

from datetime import datetime
from enum import Enum
from typing import NamedTuple


class LifecycleCommandStatus(Enum):
    """Status of a lifecycle command."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class LifecycleEventLevel(Enum):
    """Level/severity of a lifecycle event."""

    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LifecycleEvent(NamedTuple):
    """A single lifecycle event (log message)."""

    id: str
    command_id: str
    level: LifecycleEventLevel
    message: str
    created_at: datetime


class LifecycleCommand(NamedTuple):
    """A lifecycle command object representing a package lifecycle action."""

    id: str
    package_installation_id: str
    package_name: str
    version: str
    status: LifecycleCommandStatus
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None
    # The id of the command this one was spawned from in a dep-resolver chain.
    # None on the root command of a chain (or for any single-package install).
    parent_command_id: str | None = None
    # True when the install bypassed validation gates (SHACL, unresolved imports).
    force: bool = False


class LifecycleCommandResponse(NamedTuple):
    """Response from lifecycle command API."""

    command_id: str
    package_name: str
    version: str
    status: str


class ValidationEntry(NamedTuple):
    """A single validation result entry (violation, warning, or info)."""

    focus_node: str | None
    path: str | None
    value: str | None
    message: str | None


class ValidationReport(NamedTuple):
    """SHACL validation report from backend."""

    conforms: bool
    violation_count: int
    warning_count: int
    info_count: int
    violations: list[ValidationEntry]
    warnings: list[ValidationEntry]
    infos: list[ValidationEntry]
