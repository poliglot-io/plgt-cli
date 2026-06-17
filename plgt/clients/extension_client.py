"""Extension API client for Platform Service integration.

This module provides a client for interacting with the matrix extension API endpoints.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from plgt.core.exceptions import ServiceError
from plgt.core.sessions import APISession

logger = logging.getLogger(__name__)


@dataclass
class Extension:
    """Matrix extension data model."""

    id: str
    label: str
    target_matrix_uri: str | None
    active: bool
    owner_id: str | None
    owner_username: str | None
    content: str | None
    created_at: datetime
    updated_at: datetime


class ExtensionClient:
    """Client for matrix extension API operations."""

    def __init__(self, session: APISession):
        """Initialize the extension client with an API session.

        Args:
            session: Authenticated API session for making requests
        """
        self.session = session

    def create_extension(
        self,
        workspace: str,
        target_matrix: str,
        file_path: Path,
        *,
        label: str | None = None,
    ) -> Extension:
        """Create a new matrix extension.

        Creates the extension with active=false and triggers partial re-deployment
        of the target matrix. The extension becomes active after successful assembly.

        Args:
            workspace: The workspace slug
            target_matrix: The target matrix URI to extend
            file_path: Path to the Turtle content file
            label: Optional label for the extension

        Returns:
            Extension object with created extension data

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Creating extension for matrix %s in workspace %s",
            target_matrix,
            workspace,
        )

        file_content = file_path.read_bytes()

        files = {
            "file": (
                file_path.name,
                file_content,
                "text/turtle",
            ),
        }

        data = {
            "targetMatrix": target_matrix,
        }
        if label:
            data["label"] = label

        try:
            response = self.session.post(
                f"/api/v1/extensions/{workspace}",
                files=files,
                data=data,
            )

            result = response.json()

            # Handle API response wrapper
            if "data" in result:
                result = result["data"]

            return self._parse_extension(result)

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to create extension")
            msg = f"Failed to create extension: {e}"
            raise ServiceError(msg) from e

    def list_extensions(self, workspace: str) -> list[Extension]:
        """List all extensions in a workspace.

        Requires admin permissions.

        Args:
            workspace: The workspace slug

        Returns:
            List of Extension objects

        Raises:
            ServiceError: If the request fails
        """
        logger.debug("Listing extensions in workspace %s", workspace)

        try:
            response = self.session.get(f"/api/v1/extensions/{workspace}")

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            extensions_data = data if isinstance(data, list) else []
            return [self._parse_extension(e) for e in extensions_data]

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to list extensions in workspace %s", workspace)
            msg = f"Failed to list extensions: {e}"
            raise ServiceError(msg) from e

    def get_extension(self, workspace: str, extension_id: str) -> Extension:
        """Get a single extension by ID with content.

        Args:
            workspace: The workspace slug
            extension_id: The extension ID

        Returns:
            Extension object with content

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Fetching extension %s in workspace %s",
            extension_id,
            workspace,
        )

        try:
            response = self.session.get(
                f"/api/v1/extensions/{workspace}/{extension_id}"
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return self._parse_extension(data)

        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch extension %s in workspace %s",
                extension_id,
                workspace,
            )
            msg = f"Failed to fetch extension {extension_id}: {e}"
            raise ServiceError(msg) from e

    def update_extension(
        self,
        workspace: str,
        extension_id: str,
        file_path: Path | None = None,
        *,
        label: str | None = None,
    ) -> Extension:
        """Update an extension's content and/or label.

        Sets active=false and triggers partial re-deployment of the target matrix.

        Args:
            workspace: The workspace slug
            extension_id: The extension ID
            file_path: Optional path to the new Turtle content file
            label: Optional new label

        Returns:
            Updated Extension object

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Updating extension %s in workspace %s",
            extension_id,
            workspace,
        )

        # Build multipart form data - server requires multipart/form-data
        # Use tuple syntax (None, value) to send form fields in multipart format
        files = {}
        if file_path:
            file_content = file_path.read_bytes()
            files["file"] = (
                file_path.name,
                file_content,
                "text/turtle",
            )

        if label:
            # Tuple format forces multipart encoding for non-file fields
            files["label"] = (None, label)

        if not files:
            # Nothing to update
            msg = "Either file_path or label must be provided"
            raise ValueError(msg)

        try:
            response = self.session.put(
                f"/api/v1/extensions/{workspace}/{extension_id}",
                files=files,
            )

            result = response.json()

            # Handle API response wrapper
            if "data" in result:
                result = result["data"]

            return self._parse_extension(result)

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to update extension %s", extension_id)
            msg = f"Failed to update extension: {e}"
            raise ServiceError(msg) from e

    def delete_extension(self, workspace: str, extension_id: str) -> None:
        """Delete an extension.

        Triggers partial re-deployment of the target matrix.

        Args:
            workspace: The workspace slug
            extension_id: The extension ID

        Raises:
            ServiceError: If the request fails
        """
        logger.debug(
            "Deleting extension %s in workspace %s",
            extension_id,
            workspace,
        )

        try:
            self.session.delete(f"/api/v1/extensions/{workspace}/{extension_id}")

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to delete extension %s", extension_id)
            msg = f"Failed to delete extension: {e}"
            raise ServiceError(msg) from e

    def _parse_extension(self, data: dict) -> Extension:
        """Parse extension data from API response.

        Args:
            data: Raw extension data from API

        Returns:
            Extension object
        """
        target_matrix = data.get("targetMatrix") or {}
        owner = data.get("owner") or {}
        return Extension(
            id=data["id"],
            label=data["label"],
            target_matrix_uri=target_matrix.get("uri"),
            active=data.get("active", False),
            owner_id=owner.get("id"),
            owner_username=owner.get("username"),
            content=data.get("content"),
            created_at=self._parse_datetime(data["createdAt"]),
            updated_at=self._parse_datetime(data["updatedAt"]),
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
