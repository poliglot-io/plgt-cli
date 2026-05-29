"""Registry API client.

Reads published package metadata: version history, per-version variable +
secret declarations, engine-compatible version lists, URI-to-package
resolution, and archive downloads. Used by:

* the workspace-install flow to validate ``--var`` and ``--secret-from-env``
  flags against a published package's declared bindings, and
* the local-deps flow (bare ``plgt sync``) to resolve and download package
  archives into ``.matrix/deps/``.

The endpoints used here are anonymous: registry reads are public; auth is
only required for publishing and workspace-targeted actions.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from plgt.core.exceptions import ResourceNotFoundError, ServiceError
from plgt.core.sessions import APISession
from plgt.services.bindings import (
    SecretDeclaration,
    VariableDeclaration,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistryVersionRef:
    """A single engine-compatible version of a registry package.

    Wire shape: ``{version, engineVersion, publishedAt, changelog, dependencies}``
    from ``/api/v1/registry/packages/{pub}/{name}/versions``. We only retain
    the fields the CLI dep resolver and validator actually use.
    """

    version: str
    engine_version: str


@dataclass(frozen=True)
class NamespaceResolution:
    """Result of resolving a matrix namespace URI to a registry package.

    Returned by ``/api/v1/registry/resolve``. ``versions`` is filtered to
    versions whose ``engineVersion`` overlaps the requesting range, ordered
    newest first.
    """

    publisher_slug: str
    name: str
    versions: list[RegistryVersionRef]


class RegistryClient:
    """Read-only client for the platform's registry endpoints."""

    def __init__(self, session: APISession):
        self.session = session

    def get_versions(self, publisher: str, name: str) -> list[str]:
        """Return all published versions of ``publisher/name``, newest first.

        Order follows ``publishedAt`` descending as returned by the platform,
        not raw semver, which matches the registry-history sort and is what
        the upgrade dropdown uses.
        """
        try:
            response = self.session.get(
                f"/api/v1/registry/packages/{publisher}/{name}",
            )
            data = response.json()
            if "data" in data:
                data = data["data"]
            versions = data.get("versions") or []
            return [v["version"] for v in versions if v.get("version")]
        except ResourceNotFoundError as e:
            # APISession converts all 404 responses into ResourceNotFoundError before this method
            # sees them, so a raw HTTPError branch is unreachable. Rewrap to surface a specific,
            # actionable message (the generic "Not found." from APISession does not identify
            # which package was missing).
            msg = f"Package not found in registry: {publisher}/{name}"
            raise ResourceNotFoundError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to fetch registry package details")
            msg = f"Failed to fetch registry versions: {e}"
            raise ServiceError(msg) from e

    def get_declarations(
        self,
        publisher: str,
        name: str,
        version: str,
    ) -> tuple[list[VariableDeclaration], list[SecretDeclaration]]:
        """Return declared variables and secrets for a published version.

        Mirrors ``ProjectDeclarations.variables/secrets`` shape so the bindings
        validation path can be shared between local-build and registry installs.
        """
        try:
            response = self.session.get(
                f"/api/v1/registry/packages/{publisher}/{name}/{version}/declarations",
            )
            data = response.json()
            if "data" in data:
                data = data["data"]

            raw_vars = data.get("declaredVariables") or []
            raw_secrets = data.get("declaredSecrets") or []

            variables = [
                VariableDeclaration(
                    uri=v["uri"],
                    variable_type=v.get("variableType"),
                    label=v.get("label"),
                    description=v.get("description"),
                    required=bool(v.get("required", True)),
                )
                for v in raw_vars
            ]
            secrets = [
                SecretDeclaration(
                    uri=s["uri"],
                    label=s.get("label"),
                    description=s.get("description"),
                    required=bool(s.get("required", True)),
                )
                for s in raw_secrets
            ]
            return variables, secrets
        except ResourceNotFoundError as e:
            msg = f"Version not found in registry: {publisher}/{name}@{version}"
            raise ResourceNotFoundError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to fetch declarations")
            msg = f"Failed to fetch declarations: {e}"
            raise ServiceError(msg) from e

    def list_compatible_versions(
        self,
        publisher: str,
        name: str,
        engine_version: str | None = None,
    ) -> list[RegistryVersionRef]:
        """Return engine-compatible versions of ``publisher/name``, newest first.

        Hits ``GET /api/v1/registry/packages/{pub}/{name}/versions``. When
        ``engine_version`` is supplied, the registry filters to versions whose
        own engineVersion range overlaps the requested range. Without the
        filter, all published versions are returned.

        Used by the dep resolver to pick a concrete version per declared dep
        range from ``poliglot.yml``'s ``dependencies:``.
        """
        # Page through results — dep resolution must see every compatible
        # version, so capping at the server's per-page max would silently drop
        # candidates. The platform's max page size is 100; assemble pages
        # until totalPages is exhausted.
        all_versions: list[RegistryVersionRef] = []
        page = 0
        try:
            while True:
                params: dict[str, str] = {
                    "page": str(page),
                    "size": "100",
                }
                if engine_version:
                    params["engineVersion"] = engine_version
                response = self.session.get(
                    f"/api/v1/registry/packages/{publisher}/{name}/versions",
                    params=params,
                )
                payload = response.json()
                paged = payload.get("data") if "data" in payload else payload
                items = (paged or {}).get("items", []) or []
                all_versions.extend(
                    RegistryVersionRef(
                        version=v["version"],
                        engine_version=v.get("engineVersion", ""),
                    )
                    for v in items
                    if v.get("version")
                )
                total_pages = (paged or {}).get("totalPages", 0)
                if page + 1 >= total_pages:
                    break
                page += 1
            return all_versions
        except ResourceNotFoundError as e:
            msg = f"Package not found in registry: {publisher}/{name}"
            raise ResourceNotFoundError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to list compatible versions")
            msg = f"Failed to list compatible versions: {e}"
            raise ServiceError(msg) from e

    def resolve_namespace_uri(
        self,
        namespace_uri: str,
        engine_version: str | None = None,
    ) -> NamespaceResolution | None:
        """Resolve a matrix namespace URI to its providing package.

        Hits ``GET /api/v1/registry/resolve?uri=...&engineVersion=...``. Used
        by ``plgt validate`` to confirm an imported URI belongs to a package
        declared in the project's ``dependencies:``.

        Returns ``None`` if no package in the registry claims the URI (404
        from the platform). Other errors raise ``ServiceError``.
        """
        params: dict[str, str] = {"uri": namespace_uri}
        if engine_version:
            params["engineVersion"] = engine_version
        try:
            response = self.session.get(
                "/api/v1/registry/resolve",
                params=params,
            )
            data = response.json()
            if "data" in data:
                data = data["data"]
            versions = [
                RegistryVersionRef(
                    version=v["version"], engine_version=v.get("engineVersion", "")
                )
                for v in (data.get("versions") or [])
                if v.get("version")
            ]
            return NamespaceResolution(
                publisher_slug=data["publisherSlug"],
                name=data["name"],
                versions=versions,
            )
        except ResourceNotFoundError:
            # 404 here means the registry has no package claiming this URI. That's an expected
            # outcome the caller distinguishes from "couldn't talk to the registry," so swallow
            # and return None rather than re-raise.
            return None
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to resolve namespace URI")
            msg = f"Failed to resolve namespace URI {namespace_uri}: {e}"
            raise ServiceError(msg) from e

    def download_archive(
        self,
        publisher: str,
        name: str,
        version: str,
        destination: Path,
    ) -> str:
        """Stream a package archive to ``destination``, verify its integrity,
        and return the verified ``sha256:<hex>`` checksum.

        Hits ``GET /api/v1/registry/packages/{pub}/{name}/{version}/archive``,
        which serves the published ``.tgz`` bytes with an ``X-Archive-Checksum``
        header (``sha256:<hex>``). As bytes stream in, we accumulate a local
        SHA-256 hash and compare it to the server-reported value after the
        body is fully read. A missing header or mismatching hash raises
        ``ServiceError`` and removes the partial file: we will not return a
        checksum the caller could not have verified.

        The destination directory must already exist; the file is written
        atomically (``destination.tmp`` then rename) so a failed verification
        cannot leave a tampered archive behind.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        success = False
        try:
            # APISession.request raises on non-2xx, so by the time control
            # returns here we have an open stream we own and must close.
            response = self.session.get(
                f"/api/v1/registry/packages/{publisher}/{name}/{version}/archive",
                stream=True,
            )
            try:
                server_checksum = response.headers.get("X-Archive-Checksum", "").strip()
                if not server_checksum:
                    msg = (
                        f"Registry response missing X-Archive-Checksum for "
                        f"{publisher}/{name}@{version}; refusing to trust the archive"
                    )
                    raise ServiceError(msg)
                hasher = hashlib.sha256()
                with tmp.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            hasher.update(chunk)
                            out.write(chunk)
                local_checksum = f"sha256:{hasher.hexdigest()}"
                if local_checksum != server_checksum:
                    msg = (
                        f"Checksum mismatch for {publisher}/{name}@{version}: "
                        f"server reported {server_checksum} but downloaded bytes "
                        f"hash to {local_checksum}"
                    )
                    raise ServiceError(msg)
            finally:
                response.close()
            tmp.replace(destination)
            success = True
            return local_checksum
        except ResourceNotFoundError as e:
            msg = f"Version not found in registry: {publisher}/{name}@{version}"
            raise ResourceNotFoundError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception("Failed to download archive")
            msg = f"Failed to download archive: {e}"
            raise ServiceError(msg) from e
        finally:
            # Clean up the tmp file on any failure path (HTTP, network, OS
            # write error). On success the tmp was already renamed away.
            if not success and tmp.exists():
                tmp.unlink()
