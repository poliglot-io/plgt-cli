"""Unit tests for ``plgt.services.deps_lockfile``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from plgt.services.deps_lockfile import (
    LOCKFILE_FORMAT_VERSION,
    ORIGIN_REGISTRY_FALLBACK,
    ORIGIN_UNKNOWN,
    ORIGIN_WORKSPACE_PINNED,
    LockedPackage,
    Lockfile,
    read_lockfile,
    write_lockfile,
)


def _engine() -> LockedPackage:
    return LockedPackage(
        publisher="poliglot",
        name="os",
        version="2.1.3",
        checksum="sha256:engine",
        root=True,
        via=None,
        origin=ORIGIN_REGISTRY_FALLBACK,
    )


def _dep(
    publisher: str,
    name: str,
    version: str,
    *,
    root: bool = True,
    via: str | None = None,
    origin: str = ORIGIN_REGISTRY_FALLBACK,
) -> LockedPackage:
    return LockedPackage(
        publisher=publisher,
        name=name,
        version=version,
        checksum=f"sha256:{name}-{version}",
        root=root,
        via=via,
        origin=origin,
    )


class TestWriteRoundTrip:
    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        lockfile = Lockfile(
            engine=_engine(),
            dependencies=[
                _dep("widget", "widget", "1.5.0", origin=ORIGIN_WORKSPACE_PINNED),
                _dep("acme", "shared", "0.3.2", root=False, via="widget/widget"),
            ],
        )

        path = tmp_path / "test.lock"
        written = write_lockfile(path, lockfile)
        assert written == path

        loaded = read_lockfile(path)
        assert loaded == lockfile

    def test_returns_none_when_lockfile_absent(self, tmp_path: Path) -> None:
        assert read_lockfile(tmp_path / "nope.lock") is None

    def test_writes_creates_parent_directory(self, tmp_path: Path) -> None:
        lockfile = Lockfile(engine=_engine())
        path = tmp_path / ".matrix" / "deps" / "dev.lock"
        write_lockfile(path, lockfile)
        assert path.is_file()

    def test_omits_via_when_root(self, tmp_path: Path) -> None:
        lockfile = Lockfile(
            engine=_engine(), dependencies=[_dep("foo", "bar", "1.0.0")]
        )
        path = write_lockfile(tmp_path / "f.lock", lockfile)
        text = path.read_text()
        assert "via:" not in text

    def test_includes_via_for_transitive_deps(self, tmp_path: Path) -> None:
        lockfile = Lockfile(
            engine=_engine(),
            dependencies=[
                _dep("foo", "bar", "1.0.0", root=False, via="parent/pkg"),
            ],
        )
        path = write_lockfile(tmp_path / "f.lock", lockfile)
        text = path.read_text()
        assert "via: parent/pkg" in text
        assert "root: false" in text

    def test_writes_origin_for_every_entry(self, tmp_path: Path) -> None:
        lockfile = Lockfile(
            engine=_engine(),
            dependencies=[
                _dep("a", "b", "1.0.0", origin=ORIGIN_WORKSPACE_PINNED),
                _dep("c", "d", "2.0.0", origin=ORIGIN_REGISTRY_FALLBACK),
            ],
        )
        path = write_lockfile(tmp_path / "f.lock", lockfile)
        text = path.read_text()
        assert f"origin: {ORIGIN_WORKSPACE_PINNED}" in text
        assert f"origin: {ORIGIN_REGISTRY_FALLBACK}" in text


class TestReadValidation:
    def test_rejects_unsupported_format_version(self, tmp_path: Path) -> None:
        path = tmp_path / "f.lock"
        path.write_text(f"version: {LOCKFILE_FORMAT_VERSION + 999}\nengine: {{}}\n")
        with pytest.raises(ValueError, match="unsupported format version"):
            read_lockfile(path)

    def test_rejects_missing_engine_section(self, tmp_path: Path) -> None:
        path = tmp_path / "f.lock"
        path.write_text(f"version: {LOCKFILE_FORMAT_VERSION}\ndependencies: []\n")
        with pytest.raises(ValueError, match="missing the required `engine`"):
            read_lockfile(path)

    def test_rejects_package_missing_required_field(self, tmp_path: Path) -> None:
        path = tmp_path / "f.lock"
        path.write_text(
            f"version: {LOCKFILE_FORMAT_VERSION}\n"
            "engine:\n"
            "  publisher: poliglot\n"
            "  name: plgt\n"
            "  version: 2.1.3\n"
            # checksum intentionally missing
            "dependencies: []\n"
        )
        with pytest.raises(ValueError, match="missing required field `checksum`"):
            read_lockfile(path)

    def test_rejects_non_list_dependencies(self, tmp_path: Path) -> None:
        path = tmp_path / "f.lock"
        path.write_text(
            f"version: {LOCKFILE_FORMAT_VERSION}\n"
            "engine:\n"
            "  publisher: poliglot\n"
            "  name: plgt\n"
            "  version: 2.1.3\n"
            "  checksum: sha256:x\n"
            "dependencies: not-a-list\n"
        )
        with pytest.raises(ValueError, match="must be a list"):
            read_lockfile(path)

    def test_rejects_unknown_origin_value(self, tmp_path: Path) -> None:
        path = tmp_path / "f.lock"
        path.write_text(
            f"version: {LOCKFILE_FORMAT_VERSION}\n"
            "engine:\n"
            "  publisher: poliglot\n"
            "  name: plgt\n"
            "  version: 2.1.3\n"
            "  checksum: sha256:x\n"
            "  origin: bogus\n"
            "dependencies: []\n"
        )
        with pytest.raises(ValueError, match="unknown origin"):
            read_lockfile(path)


class TestV1Migration:
    """v1 lockfiles (no ``origin`` field) read back as ``unknown`` so the next
    install rewrites them in v2 shape with real origin tags.
    """

    def test_v1_engine_origin_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "v1.lock"
        path.write_text(
            "version: 1\n"
            "engine:\n"
            "  publisher: poliglot\n"
            "  name: plgt\n"
            "  version: 2.1.3\n"
            "  checksum: sha256:e\n"
            "  root: true\n"
            "dependencies: []\n"
        )
        loaded = read_lockfile(path)
        assert loaded is not None
        # Per-entry origins from v1 read as ``unknown`` (no field in v1 schema). The
        # in-memory object is forced to the current format version so any read-then-rewrite
        # path persists in v2; this prevents accidental v1 leakage.
        assert loaded.engine.origin == ORIGIN_UNKNOWN
        assert loaded.format_version == LOCKFILE_FORMAT_VERSION

    def test_v1_read_then_write_persists_as_v2(self, tmp_path: Path) -> None:
        """Read a v1 file, write it back, confirm it's now v2."""
        v1_path = tmp_path / "v1.lock"
        v1_path.write_text(
            "version: 1\n"
            "engine:\n"
            "  publisher: poliglot\n"
            "  name: plgt\n"
            "  version: 2.1.3\n"
            "  checksum: sha256:e\n"
            "  root: true\n"
            "dependencies: []\n"
        )
        loaded = read_lockfile(v1_path)
        assert loaded is not None

        v2_path = tmp_path / "v2.lock"
        write_lockfile(v2_path, loaded)
        text = v2_path.read_text()
        assert f"version: {LOCKFILE_FORMAT_VERSION}" in text
