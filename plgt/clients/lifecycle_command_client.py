"""Lifecycle Command API client for Package Service integration.

This module provides a client for interacting with the lifecycle command API endpoints.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import requests

from plgt.core.exceptions import ResourceNotFoundError, ServiceError
from plgt.core.sessions import APISession
from plgt.models.lifecycle_command import (
    LifecycleCommand,
    LifecycleCommandResponse,
    LifecycleCommandStatus,
    LifecycleEvent,
    LifecycleEventLevel,
    ValidationEntry,
    ValidationReport,
)

logger = logging.getLogger(__name__)


class LifecycleCommandClient:
    """Client for lifecycle command API operations."""

    def __init__(self, session: APISession):
        """Initialize the lifecycle command client with an API session.

        Args:
            session: Authenticated API session for making requests
        """
        self.session = session

    def install_package(
        self,
        workspace: str,
        package_file: Path,
        *,
        force_update: bool = False,
        variable_bindings: list[dict] | None = None,
        secret_bindings: list[dict] | None = None,
    ) -> LifecycleCommandResponse:
        """Deploy a package to a workspace.

        Args:
            workspace: The workspace identifier
            package_file: Path to the package.tgz file
            force_update: Whether to force update existing matrices
            variable_bindings: Optional list of variable bindings
                (each item: ``{"uri": str, "value": str, "sourceMatrix": str | None}``).
                Sent as a multipart "bindings" JSON field alongside the upload.
            secret_bindings: Optional list of E2E-encrypted secret bindings
                (each item: ``{"uri", "keyId", "clientPublicKey", "encryptedValue", "nonce"}``).

        Returns:
            LifecycleCommandResponse with command ID and initial status

        Raises:
            ServiceError: If the install request fails
        """
        import json

        logger.debug(
            "Deploying package %s to workspace %s (force_update=%s, "
            "variable_bindings=%s, secret_bindings=%s)",
            package_file.name,
            workspace,
            force_update,
            len(variable_bindings) if variable_bindings else 0,
            len(secret_bindings) if secret_bindings else 0,
        )

        file_content = package_file.read_bytes()

        files: dict[str, tuple[str, bytes | str, str] | tuple[None, str, str]] = {
            "file": (
                package_file.name,
                file_content,
                "application/gzip",
            ),
        }

        # Bindings ride alongside the tarball as a JSON multipart field so the
        # platform's direct-upload install endpoint can read both with a single
        # `multipart/form-data` parse. Empty bindings are omitted to keep the
        # request shape unchanged for existing callers.
        bindings_payload: dict[str, list[dict]] = {}
        if variable_bindings:
            bindings_payload["variableBindings"] = variable_bindings
        if secret_bindings:
            bindings_payload["secretBindings"] = secret_bindings
        if bindings_payload:
            files["bindings"] = (
                None,
                json.dumps(bindings_payload),
                "application/json",
            )

        params = {"forceUpdate": str(force_update).lower()}

        try:
            response = self.session.post(
                f"/api/v1/packages/{workspace}/install",
                files=files,
                params=params,
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return LifecycleCommandResponse(
                command_id=data["id"],
                package_name=data["packageName"],
                version=data["version"],
                status=data["status"],
            )

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to install package")
            msg = f"Failed to install package: {e}"
            raise ServiceError(msg) from e

    def install_from_registry(
        self,
        workspace: str,
        publisher: str,
        name: str,
        *,
        version: str | None = None,
        auto_update: bool | None = None,
        variable_bindings: list[dict] | None = None,
        secret_bindings: list[dict] | None = None,
    ) -> LifecycleCommandResponse:
        """Install a package from the registry by (publisher, name).

        The platform resolves the version (latest compatible with the workspace
        engine when ``version`` is omitted), pre-stages the artifact, and
        publishes a lifecycle command.

        Args:
            workspace: The workspace identifier
            publisher: Publisher slug owning the package
            name: Package name within the publisher
            version: Optional pinned version. Defaults to the latest version
                compatible with the workspace engine.
            auto_update: Optional auto-update flag for the resulting
                PackageInstallation. ``None`` lets the platform default apply
                (system packages: forced ``true``; others: ``false``).
            variable_bindings: Optional list of variable bindings (each item:
                ``{"uri", "value", "sourceMatrix"}``). Validated against the
                version's declarations server-side.
            secret_bindings: Optional list of E2E-encrypted secret bindings
                (each item:
                ``{"uri", "keyId", "clientPublicKey", "encryptedValue", "nonce"}``).

        Returns:
            LifecycleCommandResponse with command ID and initial status.

        Raises:
            ResourceNotFoundError: Workspace, publisher, or package not found
            ValidationError: Engine version incompatible (400) or active
                operation in progress (409)
            ServiceError: If the request fails
        """
        logger.debug(
            "Registry install %s/%s@%s into workspace %s "
            "(auto_update=%s, vars=%s, secrets=%s)",
            publisher,
            name,
            version or "latest",
            workspace,
            auto_update,
            len(variable_bindings) if variable_bindings else 0,
            len(secret_bindings) if secret_bindings else 0,
        )

        body: dict[str, object] = {}
        if version is not None:
            body["version"] = version
        if auto_update is not None:
            body["autoUpdate"] = auto_update
        if variable_bindings:
            body["variableBindings"] = variable_bindings
        if secret_bindings:
            body["secretBindings"] = secret_bindings

        try:
            response = self.session.post(
                f"/api/v1/packages/{workspace}/registry/{publisher}/{name}/install",
                json=body,
            )

            data = response.json()
            if "data" in data:
                data = data["data"]

            return LifecycleCommandResponse(
                command_id=data["id"],
                package_name=data.get("packageName", name),
                version=data.get("version", version or ""),
                status=data["status"],
            )

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to install package from registry")
            msg = f"Failed to install package from registry: {e}"
            raise ServiceError(msg) from e

    def uninstall_package(
        self,
        workspace: str,
        package_name: str,
    ) -> LifecycleCommandResponse:
        """Uninstall a package from a workspace.

        Args:
            workspace: The workspace identifier
            package_name: The package name to uninstall

        Returns:
            LifecycleCommandResponse with command ID and initial status

        Raises:
            ResourceNotFoundError: If the package is not installed
            ValidationError: If the package has an active operation (409)
            ServiceError: If the request fails
        """
        logger.debug(
            "Uninstalling package %s from workspace %s",
            package_name,
            workspace,
        )

        try:
            response = self.session.delete(
                f"/api/v1/packages/{workspace}/{package_name}"
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return LifecycleCommandResponse(
                command_id=data["id"],
                package_name=data.get("packageName", package_name),
                version=data.get("version", ""),
                status=data["status"],
            )

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to uninstall package")
            msg = f"Failed to uninstall package: {e}"
            raise ServiceError(msg) from e

    def set_auto_update(
        self,
        workspace: str,
        package_name: str,
        auto_update: bool,
    ) -> dict:
        """Toggle a PackageInstallation's auto-update flag.

        Args:
            workspace: The workspace identifier
            package_name: The installed package name
            auto_update: New auto-update flag

        Returns:
            Updated PackageInstallation as a dict.

        Raises:
            ResourceNotFoundError: If the package is not installed
            ServiceError: If the request fails
        """
        logger.debug(
            "Setting auto-update=%s on package %s in workspace %s",
            auto_update,
            package_name,
            workspace,
        )

        try:
            response = self.session.patch(
                f"/api/v1/packages/{workspace}/{package_name}/auto-update",
                json={"autoUpdate": auto_update},
            )
            data = response.json()
            if "data" in data:
                data = data["data"]
            return data

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to set auto-update")
            msg = f"Failed to set auto-update: {e}"
            raise ServiceError(msg) from e

    def get_command(self, workspace: str, command_id: str) -> LifecycleCommand:
        """Get command status and details.

        Args:
            workspace: The workspace identifier
            command_id: The command ID

        Returns:
            LifecycleCommand object with current status

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Fetching command %s in workspace %s",
            command_id,
            workspace,
        )

        try:
            response = self.session.get(
                f"/api/v1/packages/{workspace}/commands/{command_id}"
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return self._parse_command(data)

        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch command %s in workspace %s",
                command_id,
                workspace,
            )
            msg = f"Failed to fetch command {command_id}: {e}"
            raise ServiceError(msg) from e

    def list_commands(
        self,
        workspace: str,
        package_name: str,
        page: int = 0,
        size: int = 20,
    ) -> list[LifecycleCommand]:
        """List commands for a package.

        Args:
            workspace: The workspace identifier
            package_name: The package name
            page: Page number (0-indexed)
            size: Page size

        Returns:
            List of LifecycleCommand objects ordered by creation date (newest first)

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Listing commands for package %s in workspace %s (page=%d, size=%d)",
            package_name,
            workspace,
            page,
            size,
        )

        try:
            response = self.session.get(
                f"/api/v1/packages/{workspace}/{package_name}/commands",
                params={"page": page, "size": size},
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            # Backend returns array of commands
            commands_data = data if isinstance(data, list) else []

            return [self._parse_command(d) for d in commands_data]

        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to list commands for package %s in workspace %s",
                package_name,
                workspace,
            )
            msg = f"Failed to list commands for package {package_name}: {e}"
            raise ServiceError(msg) from e

    def get_command_events(
        self,
        workspace: str,
        command_id: str,
    ) -> list[LifecycleEvent]:
        """Get events for a specific command.

        Args:
            workspace: The workspace identifier
            command_id: The command ID

        Returns:
            List of lifecycle events

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Fetching events for command %s in workspace %s",
            command_id,
            workspace,
        )

        try:
            response = self.session.get(
                f"/api/v1/packages/{workspace}/commands/{command_id}/events"
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            # Backend may return array directly or wrapped
            events_data = data if isinstance(data, list) else data.get("events", [])

            return [self._parse_command_event(event_data) for event_data in events_data]

        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch events for command %s in workspace %s",
                command_id,
                workspace,
            )
            msg = f"Failed to fetch events for command {command_id}: {e}"
            raise ServiceError(msg) from e

    def _parse_command(self, data: dict) -> LifecycleCommand:
        """Parse command data from API response.

        Args:
            data: Raw command data from API

        Returns:
            LifecycleCommand object
        """
        # packageInstallation is absent on PENDING / FAILED-pre-commit commands (the reference is
        # set only once the installation exists), and parentCommand is absent on the root of a
        # chain. Both arrive as nested objects carrying their uri/id; default to {} so a
        # null/absent reference doesn't KeyError-crash the parser the moment a fresh command shows
        # up.
        package_installation = data.get("packageInstallation") or {}
        parent_command = data.get("parentCommand") or {}
        return LifecycleCommand(
            id=data["id"],
            package_installation_id=package_installation.get("id"),
            package_name=data["packageName"],
            version=data["version"],
            status=LifecycleCommandStatus(data["status"]),
            error_message=data.get("errorMessage"),
            created_at=self._parse_datetime(data["createdAt"]),
            updated_at=self._parse_datetime(data["updatedAt"]),
            parent_command_id=parent_command.get("id"),
            force=bool(data.get("force", False)),
        )

    def _parse_command_event(self, data: dict) -> LifecycleEvent:
        """Parse command event data from API response.

        Args:
            data: Raw event data from API

        Returns:
            LifecycleEvent object
        """
        return LifecycleEvent(
            id=data["id"],
            command_id=(data.get("command") or {}).get("id"),
            level=LifecycleEventLevel(data["level"]),
            message=data["message"],
            created_at=self._parse_datetime(data["createdAt"]),
        )

    def get_validation_report(
        self,
        workspace: str,
        command_id: str,
    ) -> ValidationReport | None:
        """Get the SHACL validation report for a command.

        Args:
            workspace: The workspace identifier
            command_id: The command ID

        Returns:
            ValidationReport object, or None if not found

        Raises:
            ServiceError: If the request fails (other than 404)
        """
        logger.debug(
            "Fetching validation report for command %s in workspace %s",
            command_id,
            workspace,
        )

        try:
            response = self.session.get(
                f"/api/v1/packages/{workspace}/commands/{command_id}/validation-report",
                headers={"Accept": "application/json"},
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return self._parse_validation_report(data)

        except ResourceNotFoundError:
            # No validation report exists for this command
            logger.debug("No validation report found for command %s", command_id)
            return None
        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch validation report for command %s",
                command_id,
            )
            msg = f"Failed to fetch validation report: {e}"
            raise ServiceError(msg) from e

    def _parse_validation_entry(self, data: dict) -> ValidationEntry:
        """Parse validation entry from API response.

        Args:
            data: Raw entry data from API

        Returns:
            ValidationEntry object
        """
        return ValidationEntry(
            focus_node=data.get("focusNode"),
            path=data.get("path"),
            value=data.get("value"),
            message=data.get("message"),
        )

    def _parse_validation_report(self, data: dict) -> ValidationReport:
        """Parse validation report from API response.

        Args:
            data: Raw report data from API

        Returns:
            ValidationReport object
        """
        return ValidationReport(
            conforms=data.get("conforms", True),
            violation_count=data.get("violationCount", 0),
            warning_count=data.get("warningCount", 0),
            info_count=data.get("infoCount", 0),
            violations=[
                self._parse_validation_entry(v) for v in data.get("violations", [])
            ],
            warnings=[
                self._parse_validation_entry(w) for w in data.get("warnings", [])
            ],
            infos=[self._parse_validation_entry(i) for i in data.get("infos", [])],
        )

    def _parse_datetime(self, value: str) -> datetime:
        """Parse datetime string from API response.

        Args:
            value: ISO format datetime string

        Returns:
            datetime object with UTC timezone
        """

        # Handle various ISO formats
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        # If naive datetime (no timezone), assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        return dt
