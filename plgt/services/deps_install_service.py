"""Local dependency resolver for ``plgt sync`` (and ``plgt add``).

Drives the npm-style local install path in one of two modes:

* **Workspace-sync** (default when a workspace context is supplied): for each
  declared dep, pin to the version the target workspace has installed. Fall
  back to registry-latest-compatible only when the workspace doesn't have
  the dep. Local-build uploads on the workspace (no registry coord) are
  treated as opaque private state — we do NOT match them against declared
  deps by name alone, because a workspace local-build of ``widget`` can't
  be reliably tied to a registry coord like ``mycompany/widget``, and
  assuming so would block legitimate registry resolution on name collision.
  See the workspace-state index logic in ``install_local_deps`` for details.
* **Registry-resolve** (fallback / explicit ``--from-registry``): resolve
  each declared dep to the latest version compatible with the project's
  ``engineVersion`` range, ignoring workspace state. Suitable for OSS-repo
  CI and exploration; produces a deterministic, anonymous cache.

Either mode:

1. Reads ``engineVersion`` and ``dependencies:`` from the project's
   ``poliglot.yml``.
2. Resolves ``engineVersion`` to a concrete ``poliglot/os`` version.
3. Resolves each declared dep (per-mode logic above).
4. Downloads each resolved archive from the public registry archive
   endpoint and extracts to a per-mode cache subtree under
   ``.matrix/deps/<workspace-slug>/`` or ``.matrix/deps/_registry/``.
5. Walks each package's own ``dependencies:`` transitively, deduplicating
   by ``(publisher, name)``. First version wins per pair; conflicting
   ranges surface as an explicit error.
6. Writes a per-mode lockfile (``.matrix/deps/<slug>.lock`` or
   ``.matrix/deps/_registry.lock``) so subsequent runs are deterministic.

Workspace-install flows (``plgt install --workspace`` for pushing) are
unaffected — they upload the local package to the platform like before.
This service is only used when the user wants to populate the local dep
cache.

The system matrix is treated as just another dep with package coord
``poliglot/os``. Validation later loads it as the foundation graph; no
special-case code path beyond knowing the constant package coord. In
workspace-sync mode the system matrix is pinned to whatever the workspace
has installed (typically the same version that's powering it), keeping
local validation in lockstep with the deployment target.
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from plgt.core.exceptions import ServiceError, ValidationError
from plgt.services.build_service import _parse_dependencies, load_project_config
from plgt.services.deps_lockfile import (
    ORIGIN_REGISTRY_FALLBACK,
    ORIGIN_WORKSPACE_PINNED,
    LockedPackage,
    Lockfile,
    read_lockfile,
    write_lockfile,
)
from plgt.utils.version_range import satisfies_range, validate_range

if TYPE_CHECKING:
    from plgt.clients.registry_client import RegistryClient
    from plgt.clients.workspace_packages_client import (
        InstalledPackageRef,
        WorkspacePackagesClient,
    )
    from plgt.models.build_types import PackageDependency

logger = logging.getLogger(__name__)

SYSTEM_PACKAGE_PUBLISHER = "poliglot"
SYSTEM_PACKAGE_NAME = "os"

DEPS_CACHE_RELATIVE_PATH = Path(".matrix") / "deps"

# Subdirectory under ``.matrix/deps/`` used by registry-resolve mode (no workspace context). A
# leading underscore avoids any possible collision with a workspace slug (slugs are
# ``[a-zA-Z0-9-]`` in our scheme).
REGISTRY_MODE_SUBDIR = "_registry"


def cache_root_for(project_dir: Path, workspace: str | None) -> Path:
    """Return the per-mode root under which packages are extracted.

    Workspace-sync mode writes under ``.matrix/deps/<workspace-slug>/``.
    Registry-resolve mode writes under ``.matrix/deps/_registry/``. The two
    subtrees are fully isolated so switching modes or workspaces does not
    churn one another's cache, and per the tenant-boundary contract a
    workspace's local-build content (when ever materialized) cannot bleed
    into another workspace's view.
    """
    subdir = REGISTRY_MODE_SUBDIR if workspace is None else workspace
    return project_dir / DEPS_CACHE_RELATIVE_PATH / subdir


def lockfile_path_for(project_dir: Path, workspace: str | None) -> Path:
    """Return the per-mode lockfile path.

    Sibling to the cache subtree, named for the mode: ``<slug>.lock`` for
    workspace-sync, ``_registry.lock`` for registry-resolve. Each mode's
    lockfile is independent; the two can coexist in the same repo.
    """
    subdir = REGISTRY_MODE_SUBDIR if workspace is None else workspace
    return project_dir / DEPS_CACHE_RELATIVE_PATH / f"{subdir}.lock"


@dataclass(frozen=True)
class InstallSummary:
    """What ``install_local_deps`` actually did. Used by the CLI for output."""

    engine: LockedPackage
    dependencies: list[LockedPackage]
    lockfile_path: Path
    fetched: list[LockedPackage]
    cached: list[LockedPackage]


def install_local_deps(
    project_dir: Path,
    registry_client: RegistryClient,
    *,
    config_filename: str = "poliglot.yml",
    transient_deps: list[PackageDependency] | None = None,
    update: bool = False,
    workspace: str | None = None,
    workspace_packages_client: WorkspacePackagesClient | None = None,
    workspace_drift_notifier: callable | None = None,
    lockfile_drift_notifier: callable | None = None,
    workspace_range_mismatch_notifier: callable | None = None,
    pin_vs_workspace_notifier: callable | None = None,
) -> InstallSummary:
    """Resolve and install every dep into the per-mode cache subtree.

    Reads ``project_dir/<config_filename>`` for ``engineVersion`` and the
    optional ``dependencies:`` map, then resolves and caches everything
    transitively. Writes a per-mode lockfile under ``.matrix/deps/``.

    ``transient_deps`` are extra root deps added in-memory for this resolution
    run only — they are NOT written to ``poliglot.yml``. Used by the
    ``plgt add <pub>/<name> --no-sync`` form so a user can pull a package
    into the cache for one-off exploration without persisting it.

    ``update=True`` forces re-resolution: cache-marker fast-paths are
    skipped so every package gets re-fetched against the registry's
    current state, and the lockfile is rewritten with the new resolution.
    Used by ``plgt sync --update``.

    ``workspace`` selects the install mode:

    * ``None`` (registry-resolve, default for OSS-repo CI): every dep is
      resolved against the registry's compatible-versions endpoint, pinned
      to the latest version satisfying the dep's declared range. Anonymous;
      no workspace state involved.
    * a slug (workspace-sync): for each declared dep, pin to the version
      the workspace has installed if any (and pull the bytes from the
      registry archive endpoint). When the workspace lacks the dep, fall
      back to registry-resolve for that one dep and notify the caller via
      ``workspace_drift_notifier``. Local-build uploads on the workspace
      (no registry coord) are skipped during workspace-state indexing —
      a local-build named ``widget`` carries no signal that it represents
      a registry coord like ``mycompany/widget``, and matching by name
      alone would block legitimate registry resolution on collision.

    When ``workspace`` is set, ``workspace_packages_client`` must be
    supplied so the resolver can query the workspace's installed list. The
    caller owns the auth context.

    ``workspace_drift_notifier``, when supplied, is invoked with a
    ``(publisher, name, range)`` tuple for each declared dep that fell
    back to the registry because the workspace did not have it installed.
    The CLI uses this to surface ``PLGT_W0902`` so the author sees the
    pre-push gap before discovering it on push.

    ``lockfile_drift_notifier``, when supplied, is invoked with a
    ``(publisher, name, pinned_version, declared_range)`` tuple for each
    coord that had a prior lockfile pin no longer satisfying the declared
    range. The CLI surfaces this as ``PLGT_W0901`` so the author knows the
    install re-resolved that coord rather than honoring the pin. Lockfile
    pins are otherwise honored as a fast path; only entries whose pinned
    version still satisfies the declared range skip the source-of-truth
    lookup (workspace or registry).

    ``workspace_range_mismatch_notifier``, when supplied, is invoked with a
    ``(publisher, name, workspace_version, declared_range)`` tuple when
    workspace-sync pins a version that does not satisfy the declared
    range. The CLI surfaces this as ``PLGT_W0903`` so the author knows
    their yml range disagrees with the deployment target. The workspace
    version still wins, since workspace is the deployment source of truth.

    ``pin_vs_workspace_notifier``, when supplied, is invoked with a
    ``(publisher, name, pinned_version, workspace_version)`` tuple when
    workspace-sync mode honors a lockfile pin whose version differs from
    what the workspace has installed (both still satisfy the declared
    range, so the pin held). Surfaces as ``PLGT_W0904``: validation will
    use the pinned version but the workspace will deploy something
    different. The author can refresh with ``plgt sync --update``.

    ``update=True`` bypasses the lockfile-pin fast path; every coord is
    re-resolved against the live registry / workspace state and the
    lockfile is rewritten.

    Returns a summary of which packages were freshly fetched vs. already
    present in the cache, so the CLI can report a meaningful diff.
    """
    if workspace is not None and workspace_packages_client is None:
        msg = (
            "install_local_deps: workspace-sync mode requires a "
            "WorkspacePackagesClient (none was supplied)"
        )
        raise ServiceError(msg)

    config_path = project_dir / config_filename
    if not config_path.exists():
        msg = f"No {config_filename} found in {project_dir}"
        raise ValidationError(msg)

    config = load_project_config(config_path)
    # `engineVersion` lives under the `package:` section per poliglot.yml
    # schema (see build_service for the canonical reader).
    package_section = config.get("package") or {}
    engine_version = package_section.get("engineVersion")
    if not engine_version or not isinstance(engine_version, str):
        msg = (
            f"{config_filename} is missing the required `package.engineVersion` "
            "field (semver range, e.g. `>=2.1 <3`)"
        )
        raise ValidationError(msg)
    validate_range(engine_version)

    declared_deps = list(_parse_dependencies(config.get("dependencies")))
    if transient_deps:
        # Append transients to the same root-dep list. Dedup against declared
        # deps so a `--no-sync` of a package already in yml just re-resolves
        # without double-listing in the lockfile.
        declared_keys = {(d.publisher, d.name) for d in declared_deps}
        declared_deps.extend(
            t for t in transient_deps if (t.publisher, t.name) not in declared_keys
        )

    workspace_state: dict[tuple[str, str], InstalledPackageRef] = {}
    if workspace is not None:
        assert workspace_packages_client is not None  # narrowed above
        installed = workspace_packages_client.list_installed(workspace)
        for ref in installed:
            # Index by (publisher, name) for registry-installed packages. Local-build uploads
            # (registry_publisher/registry_name null) have no publisher metadata, so we cannot
            # tell whether they correspond to any specific (publisher, name) the project might
            # declare. We deliberately do NOT match them by package name alone: a workspace
            # local-build of "widget" gives us no signal that it's the same package as a
            # registry coord like "mycompany/widget", and assuming so would block legitimate
            # registry resolution on name collision. Local-builds are private workspace state
            # the resolver leaves alone. Pre-push correctness for those is the user's
            # responsibility (publish, or push to the target workspace explicitly).
            if ref.registry_publisher and ref.registry_name:
                workspace_state[(ref.registry_publisher, ref.registry_name)] = ref

    # Lockfile-pin honor: when not forcing a refetch, prior pins act as a fast path. A pinned
    # version that no longer satisfies the declared range is treated as drift (PLGT_W0901) and
    # re-resolved against the current source-of-truth (workspace or registry). When update is
    # True, lockfile pins are ignored entirely so every coord re-resolves.
    existing_pins: dict[tuple[str, str], LockedPackage] = {}
    if not update:
        prior = read_lockfile(lockfile_path_for(project_dir, workspace))
        if prior is not None:
            existing_pins[(prior.engine.publisher, prior.engine.name)] = prior.engine
            for dep in prior.dependencies:
                existing_pins[(dep.publisher, dep.name)] = dep

    resolver = _Resolver(
        registry_client=registry_client,
        project_dir=project_dir,
        engine_version_range=engine_version,
        force_refetch=update,
        workspace=workspace,
        workspace_state=workspace_state,
        on_workspace_fallback=workspace_drift_notifier,
        existing_pins=existing_pins,
        on_lockfile_drift=lockfile_drift_notifier,
        on_workspace_range_mismatch=workspace_range_mismatch_notifier,
        on_pin_vs_workspace=pin_vs_workspace_notifier,
    )

    engine = resolver.resolve_engine()
    for dep in declared_deps:
        resolver.resolve_root_dep(dep)

    locked_deps = resolver.locked_deps()

    lockfile = Lockfile(engine=engine, dependencies=locked_deps)
    lockfile_path = write_lockfile(lockfile_path_for(project_dir, workspace), lockfile)

    return InstallSummary(
        engine=engine,
        dependencies=locked_deps,
        lockfile_path=lockfile_path,
        fetched=resolver.fetched_packages(),
        cached=resolver.cached_packages(),
    )


class _Resolver:
    """Per-install resolution state. Not thread-safe; one instance per run."""

    def __init__(
        self,
        *,
        registry_client: RegistryClient,
        project_dir: Path,
        engine_version_range: str,
        force_refetch: bool = False,
        workspace: str | None = None,
        workspace_state: dict[tuple[str, str], InstalledPackageRef] | None = None,
        on_workspace_fallback: callable | None = None,
        existing_pins: dict[tuple[str, str], LockedPackage] | None = None,
        on_lockfile_drift: callable | None = None,
        on_workspace_range_mismatch: callable | None = None,
        on_pin_vs_workspace: callable | None = None,
    ) -> None:
        self._client = registry_client
        self._project_dir = project_dir
        self._engine_range = engine_version_range
        self._force_refetch = force_refetch
        self._workspace = workspace
        self._workspace_state = workspace_state or {}
        self._on_workspace_fallback = on_workspace_fallback
        self._existing_pins = existing_pins or {}
        self._on_lockfile_drift = on_lockfile_drift
        self._on_workspace_range_mismatch = on_workspace_range_mismatch
        self._on_pin_vs_workspace = on_pin_vs_workspace
        # (publisher, name) -> LockedPackage. Deduplicates transitives.
        self._resolved: dict[tuple[str, str], LockedPackage] = {}
        # (publisher, name) -> origin tag used when materializing. Keyed lazily as resolutions
        # happen so _materialize can stamp the locked entry without re-deriving the source.
        self._origin_by_coord: dict[tuple[str, str], str] = {}
        # Packages we downloaded in this run (vs. already-cached entries).
        self._fetched: list[LockedPackage] = []
        self._cached: list[LockedPackage] = []

    def resolve_engine(self) -> LockedPackage:
        """Resolve the system matrix package via ``engineVersion``."""
        version, origin = self._pick_version(
            SYSTEM_PACKAGE_PUBLISHER, SYSTEM_PACKAGE_NAME, self._engine_range
        )
        self._origin_by_coord[(SYSTEM_PACKAGE_PUBLISHER, SYSTEM_PACKAGE_NAME)] = origin
        return self._materialize(
            publisher=SYSTEM_PACKAGE_PUBLISHER,
            name=SYSTEM_PACKAGE_NAME,
            version=version,
            root=True,
            via=None,
        )

    def resolve_root_dep(self, dep: PackageDependency) -> None:
        """Resolve a dep declared at the project root, then walk its transitives.

        Fast-path: if this coord is already locked in as root (same dep
        listed twice in yml, or a transitive was already promoted by an
        earlier `_resolve_package` call), skip the registry round-trip.

        Otherwise — including the case where the coord was previously seen
        only as a transitive — go through `_resolve_package`, which will
        reconcile the version against the cached entry and promote
        ``root=True``.
        """
        key = (dep.publisher, dep.name)
        existing = self._resolved.get(key)
        if existing is not None and existing.root:
            return
        version, origin = self._pick_version(dep.publisher, dep.name, dep.version_range)
        self._origin_by_coord[(dep.publisher, dep.name)] = origin
        self._resolve_package(
            publisher=dep.publisher,
            name=dep.name,
            version=version,
            root=True,
            via=None,
        )

    def _resolve_package(
        self,
        *,
        publisher: str,
        name: str,
        version: str,
        root: bool,
        via: str | None,
    ) -> LockedPackage:
        key = (publisher, name)
        existing = self._resolved.get(key)
        if existing is not None:
            if existing.version != version:
                msg = (
                    f"Conflicting versions resolved for {publisher}/{name}: "
                    f"{existing.version} (via {existing.via or 'root'}) vs "
                    f"{version} (via {via or 'root'}). Pin a compatible "
                    "version range in your poliglot.yml `dependencies:`."
                )
                raise ServiceError(msg)
            # Upgrade a transitive-only entry to root if this call is from a
            # root-dep declaration. Otherwise the lockfile would mislabel a
            # user-declared dep as "via parent" when ordering had the
            # transitive seen first.
            if root and not existing.root:
                upgraded = LockedPackage(
                    publisher=existing.publisher,
                    name=existing.name,
                    version=existing.version,
                    checksum=existing.checksum,
                    root=True,
                    via=None,
                )
                self._resolved[key] = upgraded
                return upgraded
            return existing

        locked = self._materialize(
            publisher=publisher,
            name=name,
            version=version,
            root=root,
            via=via,
        )
        self._resolved[key] = locked

        # Walk transitives: read the freshly-installed package's poliglot.yml.
        transitive_deps = self._read_package_dependencies(publisher, name, version)
        parent_coord = f"{publisher}/{name}"
        for transitive in transitive_deps:
            t_version, t_origin = self._pick_version(
                transitive.publisher, transitive.name, transitive.version_range
            )
            self._origin_by_coord[(transitive.publisher, transitive.name)] = t_origin
            self._resolve_package(
                publisher=transitive.publisher,
                name=transitive.name,
                version=t_version,
                root=False,
                via=parent_coord,
            )
        return locked

    def _materialize(
        self,
        *,
        publisher: str,
        name: str,
        version: str,
        root: bool,
        via: str | None,
    ) -> LockedPackage:
        """Ensure the package's contents are extracted into the cache. Returns
        the LockedPackage record with the resolved checksum.
        """
        version_dir = cache_dir_for(
            self._project_dir, publisher, name, version, workspace=self._workspace
        )
        marker = version_dir / ".matrix-installed"
        origin = self._origin_by_coord.get((publisher, name), ORIGIN_REGISTRY_FALLBACK)

        if marker.exists() and not self._force_refetch:
            # Already extracted. Read the recorded checksum from marker.
            checksum = marker.read_text().strip()
            if checksum:
                locked = LockedPackage(
                    publisher=publisher,
                    name=name,
                    version=version,
                    checksum=checksum,
                    root=root,
                    via=via,
                    origin=origin,
                )
                self._cached.append(locked)
                return locked
            # Empty marker → partial/corrupt install; fall through to refetch.

        # Fetch and extract.
        if version_dir.exists():
            # Partial install from a prior crash. Nuke and retry.
            shutil.rmtree(version_dir)
        version_dir.mkdir(parents=True, exist_ok=True)

        archive_path = version_dir / "package.tgz"
        logger.info("Fetching %s/%s@%s", publisher, name, version)
        checksum = self._client.download_archive(publisher, name, version, archive_path)

        # Lockfile-checksum verification: if the prior lockfile recorded a
        # checksum for this exact (publisher, name, version) tuple, the freshly
        # downloaded bytes must hash to the same value. This catches a registry
        # that quietly republishes the same version with different content
        # (would otherwise be a silent supply-chain change between syncs).
        prior_pin = self._existing_pins.get((publisher, name))
        if (
            prior_pin is not None
            and prior_pin.version == version
            and prior_pin.checksum
            and prior_pin.checksum != checksum
        ):
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(version_dir, ignore_errors=True)
            msg = (
                f"Checksum mismatch for {publisher}/{name}@{version}: lockfile "
                f"records {prior_pin.checksum} but registry now serves "
                f"{checksum}. Refusing to overwrite the cache. Run "
                f"`plgt sync --update` if the upstream change is intentional."
            )
            raise ServiceError(msg)

        with tarfile.open(archive_path, mode="r:gz") as tar:
            _safe_extract(tar, version_dir)
        archive_path.unlink()

        marker.write_text(checksum)

        locked = LockedPackage(
            publisher=publisher,
            name=name,
            version=version,
            checksum=checksum,
            root=root,
            via=via,
            origin=origin,
        )
        self._fetched.append(locked)
        return locked

    def _pick_version(
        self, publisher: str, name: str, version_range: str
    ) -> tuple[str, str]:
        """Return ``(version, origin)`` for a declared dep.

        Precedence:

        1. Lockfile pin: if a prior lockfile recorded this coord and its
           pinned version still satisfies the declared range, honor it
           (skip the source-of-truth lookup). A pin that no longer
           satisfies the range is drift: emit ``PLGT_W0901`` via
           ``on_lockfile_drift`` and fall through to fresh resolution.
        2. Workspace-sync source-of-truth: if a workspace context is
           active and the workspace has this coord installed as a
           registry-origin package, pin to that version. The workspace is
           the deployment target; we use its version even when it does
           not satisfy the declared range (surface
           ``PLGT_W0903`` via ``on_workspace_range_mismatch`` so the
           author knows yml drift exists). Local-build entries on the
           workspace have no publisher metadata and cannot be matched
           against a ``publisher/name`` declaration; they are ignored
           and resolution falls through to the registry.
        3. Registry-resolve: ask the registry for the latest version
           satisfying the dep's range. In workspace-sync mode this is the
           fallback for declared deps not installed on the workspace;
           notify the caller via ``on_workspace_fallback`` so the CLI
           surfaces ``PLGT_W0902`` for the pre-push gap.

        Registry-resolve mode (no workspace) skips steps 2; lockfile pin
        honor + registry-resolve drive the entire decision.
        """
        coord = (publisher, name)

        # Step 1: lockfile pin honor.
        pinned = self._existing_pins.get(coord)
        if pinned is not None and not self._force_refetch:
            if satisfies_range(pinned.version, version_range):
                # In workspace-sync mode, surface when the workspace has moved off the pinned
                # version (both still satisfy the range, so neither drift nor mismatch fires).
                # The author needs to know the deployment target diverged from what they're
                # validating against.
                if self._workspace is not None:
                    ws_pin = self._workspace_state.get(coord)
                    if (
                        ws_pin is not None
                        and ws_pin.current_version != pinned.version
                        and self._on_pin_vs_workspace is not None
                    ):
                        self._invoke_notifier(
                            self._on_pin_vs_workspace,
                            publisher,
                            name,
                            pinned.version,
                            ws_pin.current_version,
                        )
                return pinned.version, pinned.origin
            # Drift: pinned version no longer satisfies range. Notify and re-resolve.
            if self._on_lockfile_drift is not None:
                self._invoke_notifier(
                    self._on_lockfile_drift,
                    publisher,
                    name,
                    pinned.version,
                    version_range,
                )

        # Step 2: workspace source-of-truth.
        if self._workspace is not None:
            ws_pin = self._workspace_state.get(coord)
            if ws_pin is not None:
                if (
                    not satisfies_range(ws_pin.current_version, version_range)
                    and self._on_workspace_range_mismatch is not None
                ):
                    self._invoke_notifier(
                        self._on_workspace_range_mismatch,
                        publisher,
                        name,
                        ws_pin.current_version,
                        version_range,
                    )
                return ws_pin.current_version, ORIGIN_WORKSPACE_PINNED
            # Workspace doesn't have this dep. Fall through to registry-resolve and notify.
            if self._on_workspace_fallback is not None:
                self._invoke_notifier(
                    self._on_workspace_fallback, publisher, name, version_range
                )

        # Step 3: registry-resolve.
        candidates = self._client.list_compatible_versions(
            publisher, name, engine_version=self._engine_range
        )
        for ref in candidates:
            if satisfies_range(ref.version, version_range):
                return ref.version, ORIGIN_REGISTRY_FALLBACK
        msg = (
            f"No version of {publisher}/{name} satisfies range "
            f"`{version_range}` compatible with engineVersion `{self._engine_range}`"
        )
        raise ServiceError(msg)

    @staticmethod
    def _invoke_notifier(notifier, *args) -> None:
        """Call a best-effort UX notifier, swallowing exceptions so a flaky
        callback cannot break resolution.
        """
        try:
            notifier(*args)
        except Exception:
            logger.exception("Notifier raised; continuing resolution")

    def _read_package_dependencies(
        self, publisher: str, name: str, version: str
    ) -> list[PackageDependency]:
        """Parse the dep map from a freshly-extracted package's poliglot.yml.

        Top-level `dependencies:` is the contract for transitive deps. The
        engine package itself doesn't declare deps; user packages may.
        """
        config_path = (
            cache_dir_for(
                self._project_dir, publisher, name, version, workspace=self._workspace
            )
            / "poliglot.yml"
        )
        if not config_path.exists():
            return []
        with config_path.open() as f:
            config = yaml.safe_load(f) or {}
        return _parse_dependencies(config.get("dependencies"))

    def locked_deps(self) -> list[LockedPackage]:
        """All non-engine locked packages, sorted by (publisher, name) for stable
        lockfile output.
        """
        return sorted(self._resolved.values(), key=lambda p: (p.publisher, p.name))

    def fetched_packages(self) -> list[LockedPackage]:
        return list(self._fetched)

    def cached_packages(self) -> list[LockedPackage]:
        return list(self._cached)


def cache_dir_for(
    project_dir: Path,
    publisher: str,
    name: str,
    version: str,
    *,
    workspace: str | None = None,
) -> Path:
    """Filesystem location for an extracted package version in the local cache.

    The ``workspace`` selector picks the per-mode subtree under
    ``.matrix/deps/`` so workspace-sync and registry-resolve caches stay
    isolated. ``None`` (the default) uses the registry-resolve subtree.
    """
    return cache_root_for(project_dir, workspace) / publisher / name / version


MAX_EXTRACT_BYTES = 500 * 1024 * 1024  # 500 MiB total per archive
MAX_EXTRACT_MEMBERS = 50_000


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract ``tar`` to ``destination`` rejecting absolute paths, ``..``
    traversal, and resource-exhaustion (tar-bomb) inputs.

    Belt-and-suspenders defense in depth against malicious archives. The
    registry's archive endpoint is anonymous and any future hostile publish
    that gets through publish-time validation must not be able to write
    outside the version's cache directory or balloon disk usage.

    Limits:

    - At most ``MAX_EXTRACT_MEMBERS`` entries (default 50k).
    - At most ``MAX_EXTRACT_BYTES`` of uncompressed payload total (default
      500 MiB). Sum is computed from the tar's reported member sizes, so a
      malicious header that lies about its size will still trip the cap on
      the actual write if the OS reports back, but the primary defense is
      the pre-extract sum.
    """
    destination_resolved = destination.resolve()
    members = []
    total_bytes = 0
    for member in tar.getmembers():
        if len(members) >= MAX_EXTRACT_MEMBERS:
            msg = (
                f"Refusing to extract archive: exceeds {MAX_EXTRACT_MEMBERS} "
                "member cap (possible tar bomb)"
            )
            raise ServiceError(msg)
        # member.size is 0 for non-file types (dirs, symlinks); only regular
        # files contribute to the byte cap.
        if member.isfile():
            total_bytes += member.size
            if total_bytes > MAX_EXTRACT_BYTES:
                msg = (
                    f"Refusing to extract archive: uncompressed size "
                    f"exceeds {MAX_EXTRACT_BYTES} bytes (possible tar bomb)"
                )
                raise ServiceError(msg)
        member_path = (destination / member.name).resolve()
        try:
            member_path.relative_to(destination_resolved)
        except ValueError as e:
            msg = (
                f"Refusing to extract archive: member `{member.name}` "
                f"would escape {destination_resolved}"
            )
            raise ServiceError(msg) from e
        members.append(member)
    # `filter="data"` is the Python 3.12+ safe filter (skips device files,
    # symlinks pointing outside the archive, etc.). Forward-compat with the
    # 3.14 default flip.
    tar.extractall(destination, members=members, filter="data")


def _round_trip_yaml() -> YAML:
    """Configure a ruamel.yaml round-trip parser tuned for poliglot.yml edits.

    The defaults preserve quote style, comments, blank lines, and list-item
    indentation so editing `dependencies:` doesn't churn the rest of the file.
    """
    yml = YAML(typ="rt")
    yml.preserve_quotes = True
    # Match the convention authors hand-write: 2-space mapping indent, 4-space
    # sequence indent, sequence dashes offset by 2 (i.e. "    - item" under a
    # 2-space-indented key).
    yml.indent(mapping=2, sequence=4, offset=2)
    return yml


def _insert_dependencies_block(
    config: CommentedMap, deps_value: CommentedMap | dict
) -> None:
    """Insert a fresh top-level ``dependencies:`` key in the natural position.

    Places the block immediately after ``package:`` when present (the natural
    home for dep metadata), otherwise appends it. Blank-line separation between
    top-level keys is enforced after the dump via ``_normalize_top_level_spacing``.
    """
    if "package" in config:
        pos = list(config.keys()).index("package") + 1
        config.insert(pos, "dependencies", deps_value)
    else:
        config["dependencies"] = deps_value


def _is_top_level_key_line(line: str) -> bool:
    """Top-level key: column 0, alpha or underscore, contains a colon."""
    return bool(
        line
        and line[0] not in (" ", "\t", "-", "#")
        and ":" in line
        and (line[0].isalpha() or line[0] == "_")
    )


def _normalize_dependencies_spacing(text: str) -> str:
    """Ensure exactly one blank line above and below the ``dependencies:`` block.

    ruamel's blank-line metadata is positional and shifts when sub-keys are
    added to ``dependencies``, which strands the trailing separator inside the
    mapping. Rather than fight ruamel's comment engine, we let it emit raw
    YAML and adjust only the spacing immediately around the ``dependencies:``
    block. All other blank lines in the file (e.g. between matrix entries the
    author wrote by hand) are preserved exactly as ruamel emitted them.
    """
    lines = text.splitlines()
    deps_index: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("dependencies:") and _is_top_level_key_line(line):
            deps_index = i
            break
    if deps_index is None:
        return text

    # Find the end of the dependencies block — the next top-level key, or EOF.
    end_index = len(lines)
    for i in range(deps_index + 1, len(lines)):
        if _is_top_level_key_line(lines[i]):
            end_index = i
            break

    # Collapse any blank lines inside the deps block: ruamel sometimes carries
    # a blank that was originally after the last entry, and when a new entry is
    # appended that blank ends up between siblings inside the mapping.
    block = [lines[i] for i in range(deps_index, end_index) if lines[i].strip() != ""]

    # Trim blank lines immediately before `dependencies:` (we'll re-insert
    # exactly one).
    before_start = deps_index
    while before_start - 1 >= 0 and lines[before_start - 1].strip() == "":
        before_start -= 1

    pre = lines[:before_start]
    tail = lines[end_index:]

    out = list(pre)
    if pre and pre[-1].strip() != "":
        out.append("")  # blank between previous block and dependencies:
    out.extend(block)
    if tail:
        out.append("")  # blank between dependencies: and next top-level block
    out.extend(tail)
    return "\n".join(out) + "\n"


def add_dependency_to_config(
    config_path: Path,
    *,
    publisher: str,
    name: str,
    version_range: str,
) -> None:
    """Add or update an entry in poliglot.yml's top-level ``dependencies:`` map.

    Idempotent: if ``publisher/name`` already exists, its range is updated in
    place. Uses ruamel.yaml round-trip mode so the rest of the file (quote
    style, blank lines, comments, list indentation) is preserved. When the
    ``dependencies:`` block is newly created, it's inserted after ``package:``
    with a leading blank line so it visually separates from neighboring blocks.
    """
    yml = _round_trip_yaml()
    with config_path.open() as f:
        config = yml.load(f) or CommentedMap()

    deps = config.get("dependencies")
    coord = f"{publisher}/{name}"
    if deps is None:
        deps = CommentedMap()
        deps[coord] = version_range
        _insert_dependencies_block(config, deps)
    elif not isinstance(deps, dict):
        msg = (
            f"`dependencies:` in {config_path} is not a mapping "
            f"(found {type(deps).__name__}). Fix it by hand before re-running."
        )
        raise ValidationError(msg)
    else:
        deps[coord] = version_range

    buf = io.StringIO()
    yml.dump(config, buf)
    config_path.write_text(_normalize_dependencies_spacing(buf.getvalue()))


def remove_dependency_from_config(
    config_path: Path,
    *,
    publisher: str,
    name: str,
) -> bool:
    """Remove an entry from poliglot.yml's top-level ``dependencies:`` map.

    Returns ``True`` when the entry existed and was removed, ``False`` when
    ``dependencies:`` was empty or the entry was not present. The caller
    decides whether absence is an error (typically yes — ``plgt remove``
    should not silently no-op). A malformed ``dependencies:`` (non-mapping)
    raises, mirroring ``add_dependency_to_config`` so the failure mode is
    symmetric. Uses ruamel.yaml round-trip mode to preserve file formatting.
    """
    yml = _round_trip_yaml()
    with config_path.open() as f:
        config = yml.load(f) or CommentedMap()

    deps = config.get("dependencies")
    if deps is None:
        return False
    if not isinstance(deps, dict):
        msg = (
            f"`dependencies:` in {config_path} is not a mapping "
            f"(found {type(deps).__name__}). Fix it by hand before re-running."
        )
        raise ValidationError(msg)
    coord = f"{publisher}/{name}"
    if coord not in deps:
        return False
    del deps[coord]
    if not deps:
        # Empty `dependencies:` is removed entirely rather than left as a stub, mirroring
        # the npm convention.
        del config["dependencies"]

    buf = io.StringIO()
    yml.dump(config, buf)
    config_path.write_text(_normalize_dependencies_spacing(buf.getvalue()))
    return True
    return True


__all__ = [
    "DEPS_CACHE_RELATIVE_PATH",
    "MAX_EXTRACT_BYTES",
    "MAX_EXTRACT_MEMBERS",
    "REGISTRY_MODE_SUBDIR",
    "SYSTEM_PACKAGE_NAME",
    "SYSTEM_PACKAGE_PUBLISHER",
    "InstallSummary",
    "add_dependency_to_config",
    "cache_dir_for",
    "cache_root_for",
    "install_local_deps",
    "lockfile_path_for",
    "remove_dependency_from_config",
]
