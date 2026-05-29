"""Publisher-side registry client. Handles publish, dry-run validate, yank, unyank.

Read-only operations (version history, install declarations) live in
:mod:`plgt.clients.registry_client`. This module is purely the write surface — the
endpoints that mutate registry state under an authenticated publisher's slug.

Responses are normalized into structured exceptions so CLI commands can render
specific user-facing messages without re-parsing JSON.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from plgt.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from plgt.core.sessions import APISession

logger = logging.getLogger(__name__)

# Mirror of the server-side hard cap for paginated list endpoints. Listing the caller's own
# packages defaults to this size so `plgt list` returns the full catalog in one round trip
# for typical publishers.
DEFAULT_LIST_PAGE_SIZE = 100


class PublishClient:
    """Authenticated client for the platform's publish/yank surface."""

    def __init__(self, session: APISession):
        self.session = session

    # ----- /publishers/me -----

    def get_my_publisher(self) -> dict[str, Any]:
        """Return the caller's own publisher record.

        The slug from this response is the one the publish/yank endpoints expect in
        the URL path. We never let users provide their slug as a flag — it comes
        from the authenticated identity, period. This avoids accidental
        cross-publisher publishing and ensures the slug shown in CLI output matches
        what the server will accept.
        """
        try:
            response = self.session.get("/api/v1/registry/publishers/me")
            data = response.json()
            return data.get("data") if "data" in data else data
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                msg = "Not authenticated. Run `plgt auth login` first."
                raise AuthenticationError(msg) from e
            if e.response is not None and e.response.status_code == 404:
                msg = "No publisher provisioned for your account — contact support."
                raise ResourceNotFoundError(msg) from e
            logger.exception("Failed to fetch /publishers/me")
            msg = f"Failed to fetch your publisher: {e}"
            raise ServiceError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to fetch /publishers/me")
            msg = f"Failed to fetch your publisher: {e}"
            raise ServiceError(msg) from e

    # ----- publish -----

    def publish(
        self,
        publisher_slug: str,
        package_name: str,
        tarball_path: Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any] | None:
        """POST a tarball to the publish endpoint.

        Returns the registry version response on success, or ``None`` on a
        successful dry-run (the server returns 200 with an empty payload to
        signal "would have succeeded").

        Raises:
            ValidationError: 422 response with a structured ValidationReport in
                the ApiError.details field.
            ConflictError: 409 (version already exists with different checksum).
            AuthenticationError: 401 or 403 (auth/agreement/ownership gate).
            ServiceError: anything else.
        """
        url = (
            f"/api/v1/registry/publishers/{publisher_slug}"
            f"/packages/{package_name}/versions"
        )
        params = {"dryRun": "true"} if dry_run else None
        try:
            with tarball_path.open("rb") as f:
                files = {"file": (tarball_path.name, f, "application/gzip")}
                response = self.session.post(url, files=files, params=params)
            if dry_run and response.status_code == 200:
                return None
            data = response.json()
            return data.get("data") if "data" in data else data
        except requests.exceptions.HTTPError as e:
            self._raise_for_publish_error(e)
            # Unreachable — _raise_for_publish_error always raises.
            raise
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to POST publish")
            msg = f"Publish request failed: {e}"
            raise ServiceError(msg) from e

    @staticmethod
    def _raise_for_publish_error(error: requests.exceptions.HTTPError) -> None:
        """Map the platform's structured error responses onto plgt exceptions.

        The server returns ``ApiResponse.error(new ApiError(code, message, details))``
        where ``details`` carries a ValidationReport for 422 responses. We keep the
        full report attached so CLI rendering can show every violation.
        """
        response = error.response
        if response is None:
            msg = f"Publish failed: {error}"
            raise ServiceError(msg) from error

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        api_error = payload.get("error") if isinstance(payload, dict) else None
        code = api_error.get("code") if isinstance(api_error, dict) else None
        message = (
            api_error.get("message") if isinstance(api_error, dict) else response.text
        )
        details = api_error.get("details") if isinstance(api_error, dict) else None

        status = response.status_code
        if status == 401:
            raise AuthenticationError(
                message or "Authentication required — run `plgt auth login`."
            ) from error
        if status == 403:
            raise AuthenticationError(
                message or "Forbidden — check publisher ownership and agreement."
            ) from error
        if status == 409:
            raise ConflictError(message or "Version already published.") from error
        if status == 422:
            err = ValidationError(message or "Publish validation failed.")
            err.code = code
            err.report = details
            raise err from error
        if status == 429:
            err = ServiceError(message or "Publish rate limit exceeded.")
            err.code = "RateLimitExceeded"
            raise err from error
        if status == 404:
            raise ResourceNotFoundError(
                message or "Publisher or package not found."
            ) from error
        raise ServiceError(message or f"Publish failed with status {status}") from error

    # ----- yank / unyank -----

    def yank(
        self,
        publisher_slug: str,
        package_name: str,
        version: str,
        reason: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Yank a published version. Returns the updated version record.

        ``force`` is required when yanking the last live version of a package — the platform
        rejects with 409 otherwise (footgun guard: retiring a package via a single yank is
        almost never the intent). The CLI surfaces this via ``--force``.
        """
        url = (
            f"/api/v1/registry/publishers/{publisher_slug}"
            f"/packages/{package_name}/versions/{version}/yank"
        )
        try:
            response = self.session.post(url, json={"reason": reason, "force": force})
            data = response.json()
            return data.get("data") if "data" in data else data
        except requests.exceptions.HTTPError as e:
            self._raise_for_publish_error(e)
            raise
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to POST yank")
            msg = f"Yank request failed: {e}"
            raise ServiceError(msg) from e

    def list_my_packages(
        self, page: int = 0, size: int = DEFAULT_LIST_PAGE_SIZE
    ) -> dict[str, Any]:
        """List the caller's own published packages (paginated PagedResponse envelope).

        Powers ``plgt list``. Returns the raw PagedResponse: callers iterate items and read
        ``totalPages`` to decide whether to page further. The default page size of
        ``DEFAULT_LIST_PAGE_SIZE`` covers typical publishers in a single round trip.
        """
        url = f"/api/v1/registry/publishers/me/packages?page={page}&size={size}"
        try:
            response = self.session.get(url)
            payload = response.json()
            return payload.get("data") if "data" in payload else payload
        except requests.exceptions.HTTPError as e:
            self._raise_for_publish_error(e)
            raise
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to GET /publishers/me/packages")
            msg = f"List request failed: {e}"
            raise ServiceError(msg) from e

    def unyank(
        self, publisher_slug: str, package_name: str, version: str
    ) -> dict[str, Any]:
        """Unyank a previously-yanked version."""
        url = (
            f"/api/v1/registry/publishers/{publisher_slug}"
            f"/packages/{package_name}/versions/{version}/unyank"
        )
        try:
            response = self.session.post(url)
            data = response.json()
            return data.get("data") if "data" in data else data
        except requests.exceptions.HTTPError as e:
            self._raise_for_publish_error(e)
            raise
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to POST unyank")
            msg = f"Unyank request failed: {e}"
            raise ServiceError(msg) from e
