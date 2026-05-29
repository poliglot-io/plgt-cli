"""Data types for install operations.

This module contains NamedTuple definitions for installation-related data structures.
"""

from pathlib import Path
from typing import NamedTuple


class InstallConfig(NamedTuple):
    """Configuration for install operations."""

    config_file: Path | None = None
    workspace: str | None = None
    release_notes: str | None = None
    force: bool = False
    # Raw flag values; resolved against project declarations during the
    # install workflow (see `services/bindings.py`).
    var_flags: tuple[str, ...] = ()
    secret_from_env_flags: tuple[str, ...] = ()
    # When True, exit on 409 active-command conflict instead of attaching
    # to the in-flight install.
    no_attach: bool = False


class InstallResult(NamedTuple):
    """Result of install operations."""

    command_id: str
    status: str
    matrix_uri: str
    version: str
    artifact_file: Path
    success: bool
    error_message: str | None = None


class RegistryInstallConfig(NamedTuple):
    """Configuration for installing a published package from the registry."""

    publisher: str
    name: str
    # ``None`` means "latest published version" — resolved client-side so we
    # can fetch declarations for the matching version before sending the
    # install request.
    version: str | None = None
    workspace: str | None = None
    # Raw flag values; resolved against the registry version's declarations
    # during the install workflow (see ``services/bindings.py``).
    var_flags: tuple[str, ...] = ()
    secret_from_env_flags: tuple[str, ...] = ()
    # ``None`` lets the platform default apply (system packages: True;
    # otherwise: False).
    auto_update: bool | None = None
    # When True, exit on 409 active-command conflict instead of silently
    # attaching to the in-flight install. Mirrors local-build semantics.
    no_attach: bool = False
