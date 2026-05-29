"""Deployment-metadata discovery.

Fetches ``{base_url}/.well-known/poliglot.json`` and returns the parsed
payload as a typed dataclass. This is what makes the CLI work against any
Poliglot deployment without rebuilding: the deployment publishes its own
OAuth client_id, issuer, name, version, and minimum-compatible CLI
version, and the CLI caches them under ``[deployment]`` in
``~/.plgt/config``.

This module deliberately does not depend on ``APISession``: discovery
runs *before* authentication, against an unauthenticated public
endpoint, so it uses a fresh ``requests`` call with a short timeout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from plgt.core import settings
from plgt.core.exceptions import ServiceError, ValidationError

logger = logging.getLogger(settings.APP_AUTHOR)


# Test mocks should intercept any URL ending with this path.
WELL_KNOWN_PATH = "/.well-known/poliglot.json"

# Discovery is a small JSON fetch; we never want it to hang the CLI for
# more than a few seconds.
_DISCOVERY_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class DeploymentMetadata:
    """Parsed shape of ``/.well-known/poliglot.json``.

    Only fields the CLI currently consumes are surfaced. Unknown keys on
    the wire are ignored — the response shape can grow additively without
    requiring a CLI bump, but new keys must be promoted to typed
    attributes here before any caller can read them.
    """

    deployment_name: str
    deployment_version: str
    oauth_issuer: str
    oauth_client_id: str
    min_cli_version: str | None

    def to_config_dict(self) -> dict[str, str]:
        """Return the subset persisted under ``[deployment]``.

        Adds ``discovered_at`` (UTC ISO-8601) so subsequent commands can
        surface staleness if desired.
        """
        out: dict[str, str] = {
            "deployment_name": self.deployment_name,
            "deployment_version": self.deployment_version,
            "oauth_issuer": self.oauth_issuer,
            "oauth_client_id": self.oauth_client_id,
            "discovered_at": datetime.now(tz=UTC).isoformat(),
        }
        if self.min_cli_version:
            out["min_cli_version"] = self.min_cli_version
        return out


def discover(base_url: str) -> DeploymentMetadata:
    """Fetch and parse the deployment-metadata document for ``base_url``.

    Raises ``ServiceError`` for network / non-2xx failures and
    ``ValidationError`` when the document is missing required fields or
    is not parseable JSON. We intentionally keep the two error types
    distinct: "couldn't reach the deployment" is recoverable (retry,
    different URL), but "the deployment served garbage" is not.
    """
    if not base_url:
        msg = "base_url is required"
        raise ValidationError(msg)

    url = base_url.rstrip("/") + WELL_KNOWN_PATH
    logger.debug("Fetching deployment metadata from %s", url)

    try:
        response = requests.get(url, timeout=_DISCOVERY_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as e:
        msg = (
            f"Could not reach deployment at {base_url}: {e}. "
            f"Confirm the URL is correct and reachable."
        )
        raise ServiceError(msg) from e

    if not response.ok:
        msg = (
            f"Deployment at {base_url} did not publish a discovery document "
            f"(HTTP {response.status_code} from {WELL_KNOWN_PATH}). "
            f"Ensure the deployment is running a recent platform version."
        )
        raise ServiceError(msg)

    try:
        payload = response.json()
    except ValueError as e:
        msg = (
            f"Deployment at {base_url} returned a non-JSON discovery "
            f"document. Cannot trust this response."
        )
        raise ValidationError(msg) from e

    return _parse(payload, base_url=base_url)


def _parse(payload: dict, *, base_url: str) -> DeploymentMetadata:
    """Validate and unpack the discovery payload."""
    if not isinstance(payload, dict):
        msg = (
            f"Discovery document at {base_url} is not a JSON object "
            f"(got {type(payload).__name__})."
        )
        raise ValidationError(msg)

    oauth = payload.get("oauth")
    if not isinstance(oauth, dict):
        msg = (
            f"Discovery document at {base_url} is missing the required 'oauth' object."
        )
        raise ValidationError(msg)

    deployment_name = payload.get("deployment_name")
    deployment_version = payload.get("deployment_version")
    oauth_issuer = oauth.get("issuer")
    oauth_client_id = oauth.get("cli_client_id")

    missing = [
        name
        for name, value in (
            ("deployment_name", deployment_name),
            ("deployment_version", deployment_version),
            ("oauth.issuer", oauth_issuer),
            ("oauth.cli_client_id", oauth_client_id),
        )
        if not value
    ]
    if missing:
        msg = (
            f"Discovery document at {base_url} is missing required fields: "
            f"{', '.join(missing)}."
        )
        raise ValidationError(msg)

    return DeploymentMetadata(
        deployment_name=str(deployment_name),
        deployment_version=str(deployment_version),
        oauth_issuer=str(oauth_issuer),
        oauth_client_id=str(oauth_client_id),
        min_cli_version=str(payload["min_cli_version"])
        if payload.get("min_cli_version")
        else None,
    )


def ensure_deployment_configured() -> None:
    """Ensure ``[deployment]`` is populated in the user config.

    On first use the CLI has no cached deployment metadata. Rather than
    forcing the user to run ``plgt configure defaults`` before they can
    do anything else, lazily run discovery against ``platform_url()``,
    persist the result, and enforce ``min_cli_version``. Subsequent
    calls are a no-op once the cache is populated.

    Called at the entry point of any auth flow (see ``OAuthClient``) so
    a fresh install can go straight from ``plgt auth login`` to a working
    authenticated session.

    Raises ``ServiceError`` if the deployment is unreachable and
    ``ValidationError`` if its discovery document is malformed or
    declares an incompatible minimum CLI version.
    """
    from plgt.core import config

    if config.deployment.get("oauth_client_id"):
        return

    metadata = discover(settings.platform_url())
    enforce_min_cli_version(metadata)
    config.set_deployment(**metadata.to_config_dict())
    config.save()


def enforce_min_cli_version(metadata: DeploymentMetadata) -> None:
    """Raise ``ValidationError`` if the running CLI is older than the
    deployment's declared minimum.

    Uses lexicographic dotted-int comparison rather than full semver:
    the build-time ``APP_VERSION`` is the source of truth and is set in
    sync with the platform's own versioning. Comparing on integer
    tuples of ``major.minor.patch`` is enough; pre-release tags are
    treated as "newer than the same numeric version" by ignoring them.
    """
    declared = metadata.min_cli_version
    if not declared:
        return

    if _version_tuple(settings.APP_VERSION) < _version_tuple(declared):
        msg = (
            f"This CLI version ({settings.APP_VERSION}) is older than the "
            f"minimum required by the '{metadata.deployment_name}' "
            f"deployment ({declared}). Upgrade the CLI before continuing."
        )
        raise ValidationError(msg)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Convert a dotted-int version string to a comparable tuple.

    Non-numeric suffixes (``-beta.1``, ``+build123``) are stripped — the
    minimum-version check is coarse on purpose; nuanced compatibility
    belongs to a richer policy if/when we need it.
    """
    head = version.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for chunk in head.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            # Stop at the first non-numeric component. Treat e.g.
            # ``1.0.dev0`` as ``(1, 0)``.
            break
    return tuple(parts)
