"""Workspace package-installation API client.

Reads the workspace's installed-package state, which is the input to
workspace-sync mode of ``plgt sync``. Each installed package row carries
its identity (``name``), what version is running (``currentVersion``), and
whether it was installed from the registry (``registryPublisher`` +
``registryName`` non-null) or as a local-build upload (both null).

Workspace-sync resolution pins declared deps to the workspace's installed
version when a matching registry coord is found. A local-build-only match
hard-fails (the platform's install resolver cannot materialize an
unpublished dep on a target workspace, so the toolchain refuses to let one
slip into a validation pass that would push successfully but break at
activation).

This client is workspace-scoped and requires auth on the same surface as
the workspace's lifecycle commands. There is no cross-workspace aggregation
in the CLI: each invocation operates against a single workspace context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

from plgt.core.exceptions import (
    AuthenticationError,
    ResourceNotFoundError,
    ServiceError,
)

if TYPE_CHECKING:
    from plgt.core.sessions import APISession

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


@dataclass(frozen=True)
class InstalledPackageRef:
    """A package installation on a workspace.

    ``registry_publisher`` and ``registry_name`` are non-null when the
    installation was sourced from the registry (so the CLI can pull the same
    bytes from the public archive endpoint). Both null means the package was
    uploaded as a local build and has no registry coord; depending on such
    a package is rejected at validate time.
    """

    name: str
    current_version: str
    registry_publisher: str | None
    registry_name: str | None

    @property
    def is_registry_installed(self) -> bool:
        return self.registry_publisher is not None and self.registry_name is not None


class WorkspacePackagesClient:
    """Read-only client for a workspace's installed-package list."""

    def __init__(self, session: APISession):
        self.session = session

    def list_installed(self, workspace: str) -> list[InstalledPackageRef]:
        """Return every package installed on ``workspace``.

        Pages through ``GET /api/v1/packages/{workspace}`` until the server
        reports no further pages. The endpoint requires the caller to be
        authenticated against the workspace; an unauthed session surfaces
        as ``AuthenticationError`` via ``APISession``.
        """
        results: list[InstalledPackageRef] = []
        skipped: list[dict] = []
        page = 1
        while True:
            try:
                response = self.session.get(
                    f"/api/v1/packages/{workspace}",
                    params={"page": page, "pageSize": _PAGE_SIZE},
                )
            except ResourceNotFoundError as e:
                msg = f"Workspace not found: {workspace}"
                raise ResourceNotFoundError(msg) from e
            except AuthenticationError as e:
                # The platform returns 403 for both "workspace does not exist"
                # and "you don't have access" to avoid leaking workspace
                # existence to unauthenticated callers. Surface that ambiguity
                # in the message so the user knows to check the slug too.
                msg = (
                    f"Workspace '{workspace}' is not accessible — it may not "
                    f"exist, or your account doesn't have access. "
                    f"Run `plgt auth sync` to refresh your workspace list."
                )
                raise AuthenticationError(msg) from e
            except requests.exceptions.RequestException as e:
                logger.exception("Failed to list packages for workspace %s", workspace)
                msg = (
                    f"Failed to list installed packages on workspace '{workspace}': {e}"
                )
                raise ServiceError(msg) from e

            envelope = response.json()
            data = envelope.get("data", envelope)
            items = data.get("items") or []
            for item in items:
                # currentVersion is required for a useful entry; track skipped rows so the
                # caller can decide whether a "workspace gap" is actually data corruption.
                version = item.get("currentVersion")
                name = item.get("name")
                if not name or not version:
                    skipped.append(item)
                    continue
                results.append(
                    InstalledPackageRef(
                        name=name,
                        current_version=version,
                        registry_publisher=item.get("registryPublisher"),
                        registry_name=item.get("registryName"),
                    )
                )

            total_pages = int(data.get("totalPages") or 0)
            if total_pages <= 0 or page >= total_pages:
                break
            page += 1

        if skipped:
            # A single warning log line summarizing the count + sample. We do NOT raise
            # because the resolver can still operate on the well-formed rows; surfacing the
            # corruption count is preferable to silently dropping it (the workspace_drift
            # path would otherwise mistake a malformed row for a real gap).
            logger.warning(
                "Workspace '%s' returned %d malformed package rows (skipped). "
                "Sample: %r. The resolver may treat affected deps as missing on the "
                "workspace.",
                workspace,
                len(skipped),
                skipped[0],
            )

        return results
