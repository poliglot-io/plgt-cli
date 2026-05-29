"""Lockfile read/write for ``plgt sync``.

The lockfile is a **local validation snapshot**, not a deployment contract.
It records what the CLI resolved at the time of the last ``plgt sync``
so collaborators and CI on the matrix repo see byte-stable diagnostics
from ``plgt validate``. The deployment target (a workspace) is the source
of truth for what actually runs; the lockfile mirrors what the CLI saw,
not what the workspace will see.

One file per install mode coexists in the same repo:

* Workspace-sync: ``.matrix/deps/<workspace-slug>.lock`` (one per workspace
  the user has synced against).
* Registry-resolve: ``.matrix/deps/_registry.lock``.

Each entry carries an ``origin`` tag so the lockfile is self-describing:
``workspace-pinned`` when the CLI took the version from the workspace's
installed list, ``registry-fallback`` when the workspace did not have the
dep and the CLI fell back to the registry.

Lockfile shape::

    version: 2
    engine:
      publisher: poliglot
      name: plgt
      version: 2.1.3
      checksum: "sha256:..."
      origin: workspace-pinned
    dependencies:
      - publisher: widget
        name: widget
        version: 1.5.0
        checksum: "sha256:..."
        root: true
        origin: workspace-pinned
      - publisher: acme
        name: shared-vocab
        version: 0.3.2
        checksum: "sha256:..."
        root: false
        via: widget/widget
        origin: registry-fallback

The CLI consumes this file at every subsequent ``plgt sync``,
``plgt build``, and ``plgt validate`` invocation so the same package
versions are used across machines and CI runs without re-resolving against
the live registry. Drift between ``poliglot.yml`` and the lockfile triggers
``PLGT_W0901`` and a re-resolve. ``plgt sync --update`` re-resolves
unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# Format version 2 added the ``origin`` field. v1 lockfiles can still be read but their
# origin defaults to ``unknown``; the next ``plgt sync`` rewrites in v2 shape.
LOCKFILE_FORMAT_VERSION: Final = 2

# Allowed values for ``LockedPackage.origin``. ``workspace-pinned`` means the CLI took the
# version from the workspace's installed-package list. ``registry-fallback`` means the CLI
# resolved the version against the registry's compatible-versions endpoint (either because
# no workspace context exists or the workspace lacked the dep). ``unknown`` is the migration
# value for entries read from a v1 lockfile.
ORIGIN_WORKSPACE_PINNED: Final = "workspace-pinned"
ORIGIN_REGISTRY_FALLBACK: Final = "registry-fallback"
ORIGIN_UNKNOWN: Final = "unknown"

_VALID_ORIGINS: Final = frozenset(
    {ORIGIN_WORKSPACE_PINNED, ORIGIN_REGISTRY_FALLBACK, ORIGIN_UNKNOWN}
)


@dataclass(frozen=True)
class LockedPackage:
    """A single resolved package pinned in the lockfile.

    ``root=True`` means the package is declared directly in the project's
    ``poliglot.yml`` ``dependencies:`` (or, for the engine, derived from the
    project's ``engineVersion``). ``root=False`` means the package was
    pulled in as a transitive dep of another package; ``via`` then carries
    the parent's ``publisher/name`` for traceability.

    ``origin`` records where the version came from: ``workspace-pinned``
    or ``registry-fallback``. Lockfiles written in v1 format are migrated
    to ``unknown`` and rewritten as one of the two real values on the next
    install.
    """

    publisher: str
    name: str
    version: str
    checksum: str
    root: bool = True
    via: str | None = None
    origin: str = ORIGIN_UNKNOWN


@dataclass(frozen=True)
class Lockfile:
    """Top-level lockfile shape. ``engine`` resolves the system matrix from the
    project's ``engineVersion``; ``dependencies`` covers every other locked
    package (root + transitives).
    """

    engine: LockedPackage
    dependencies: list[LockedPackage] = field(default_factory=list)
    format_version: int = LOCKFILE_FORMAT_VERSION


def read_lockfile(path: Path) -> Lockfile | None:
    """Read a lockfile at ``path`` if it exists.

    Returns ``None`` when the lockfile is absent (first-time install or
    after ``.matrix/`` was cleaned). A malformed or unsupported-version
    lockfile is a hard error: the caller should not silently rebuild it
    because that erases provenance the user may want to inspect.

    ``path`` is the full filesystem path to the lockfile (per-mode under
    ``.matrix/deps/``). The caller resolves the mode-appropriate path via
    ``deps_install_service.lockfile_path_for``.
    """
    if not path.exists():
        return None
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return _from_dict(raw, source_path=path)


def write_lockfile(path: Path, lockfile: Lockfile) -> Path:
    """Persist ``lockfile`` to ``path``, returning the written path.

    Creates parent directories as needed. The caller is responsible for
    choosing the correct per-mode location via
    ``deps_install_service.lockfile_path_for``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(_to_dict(lockfile), f, sort_keys=False)
    return path


def _to_dict(lockfile: Lockfile) -> dict:
    return {
        "version": lockfile.format_version,
        "engine": _package_to_dict(lockfile.engine),
        "dependencies": [_package_to_dict(d) for d in lockfile.dependencies],
    }


def _package_to_dict(pkg: LockedPackage) -> dict:
    out: dict = {
        "publisher": pkg.publisher,
        "name": pkg.name,
        "version": pkg.version,
        "checksum": pkg.checksum,
        "root": pkg.root,
    }
    if pkg.via:
        out["via"] = pkg.via
    out["origin"] = pkg.origin
    return out


def _from_dict(raw: dict, *, source_path: Path) -> Lockfile:
    if not isinstance(raw, dict):
        msg = f"Lockfile {source_path} is malformed: expected a mapping at top level"
        raise ValueError(msg)  # noqa: TRY004 — value semantics, not type
    fmt = raw.get("version")
    # Read both v1 (no origin) and v2 (with origin). v1 entries are migrated to ``unknown``
    # so the next install rewrites them in v2 with the real value. Anything else is a hard
    # error: we don't know how to interpret future formats.
    if fmt not in (1, LOCKFILE_FORMAT_VERSION):
        msg = (
            f"Lockfile {source_path} has unsupported format version {fmt!r}; "
            f"expected 1 or {LOCKFILE_FORMAT_VERSION}. Delete the file and re-run "
            "`plgt sync` to regenerate."
        )
        raise ValueError(msg)

    engine_raw = raw.get("engine")
    if not isinstance(engine_raw, dict):
        msg = f"Lockfile {source_path} is missing the required `engine` section"
        raise ValueError(msg)  # noqa: TRY004 — value semantics, not type
    engine = _package_from_dict(engine_raw, source_path=source_path)

    deps_raw = raw.get("dependencies") or []
    if not isinstance(deps_raw, list):
        msg = f"Lockfile {source_path}'s `dependencies` must be a list"
        raise ValueError(msg)  # noqa: TRY004 — value semantics, not type
    dependencies = [_package_from_dict(d, source_path=source_path) for d in deps_raw]

    # v1 lockfiles are read with origin=unknown on each entry; rewriting them must come out
    # in v2 shape so the lockfile self-describes correctly. Forcing the construct here means
    # any caller that round-trips read→write through the dataclass cannot accidentally
    # persist the legacy version.
    return Lockfile(
        engine=engine, dependencies=dependencies, format_version=LOCKFILE_FORMAT_VERSION
    )


def _package_from_dict(raw: dict, *, source_path: Path) -> LockedPackage:
    for field_name in ("publisher", "name", "version", "checksum"):
        if not raw.get(field_name):
            msg = (
                f"Lockfile {source_path}: package entry missing required "
                f"field `{field_name}`"
            )
            raise ValueError(msg)
    origin = raw.get("origin", ORIGIN_UNKNOWN)
    if origin not in _VALID_ORIGINS:
        msg = (
            f"Lockfile {source_path}: package entry has unknown origin "
            f"{origin!r}; expected one of {sorted(_VALID_ORIGINS)}"
        )
        raise ValueError(msg)
    return LockedPackage(
        publisher=raw["publisher"],
        name=raw["name"],
        version=raw["version"],
        checksum=raw["checksum"],
        root=bool(raw.get("root", True)),
        via=raw.get("via"),
        origin=origin,
    )
