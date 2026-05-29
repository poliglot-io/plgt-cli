"""Unit tests for ``plgt.services.deps_install_service``."""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from plgt.clients.registry_client import RegistryVersionRef
from plgt.core.exceptions import ServiceError, ValidationError
from plgt.models.build_types import PackageDependency
from plgt.services.deps_install_service import (
    _safe_extract,
    cache_dir_for,
    install_local_deps,
    lockfile_path_for,
)
from plgt.services.deps_lockfile import read_lockfile

if TYPE_CHECKING:
    from pathlib import Path


def _write_project_config(
    project_dir: Path,
    *,
    engine_version: str | None = ">=1 <2",
    dependencies: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a minimal poliglot.yml. Sufficient for the deps resolver since
    it only reads the engineVersion and dependencies fields."""
    parts: list[str] = ['version: "1"', "package:"]
    parts.append('  name: "test-pkg"')
    parts.append('  version: "0.1.0"')
    if engine_version is not None:
        parts.append(f'  engineVersion: "{engine_version}"')
    if dependencies:
        parts.append("dependencies:")
        for coord, range_str in dependencies.items():
            parts.append(f'  "{coord}": "{range_str}"')
    if extra:
        import yaml

        parts.append(yaml.safe_dump(extra))
    (project_dir / "poliglot.yml").write_text("\n".join(parts) + "\n")


def _make_archive_bytes(internal: dict[str, str]) -> bytes:
    """Build an in-memory .tgz containing ``internal`` (path -> text content)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in internal.items():
            data = content.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _stub_download_to_disk(archive_bytes: bytes, checksum: str = "sha256:test"):
    """Build a fake ``download_archive`` that writes the supplied bytes to the
    requested destination path and returns the supplied checksum.
    """

    def _stub(publisher, name, version, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive_bytes)
        return checksum

    return _stub


def _engine_archive() -> bytes:
    return _make_archive_bytes(
        {"spec/plgt/matrix.ttl": "# system matrix placeholder\n"}
    )


def _user_archive(coord: str, transitive_deps: dict[str, str] | None = None) -> bytes:
    """Generate a fake user-matrix package with optional transitive deps."""
    parts = ['version: "1"', "package:"]
    parts.append(f'  name: "{coord.rsplit("/", maxsplit=1)[-1]}"')
    parts.append('  version: "1.0.0"')
    parts.append('  engineVersion: ">=1 <2"')
    if transitive_deps:
        parts.append("dependencies:")
        for c, r in transitive_deps.items():
            parts.append(f'  "{c}": "{r}"')
    poliglot_yml = "\n".join(parts) + "\n"
    return _make_archive_bytes(
        {"poliglot.yml": poliglot_yml, "spec/matrix.ttl": "# placeholder\n"}
    )


class TestInstallLocalDeps:
    def test_rejects_missing_config(self, tmp_path: Path) -> None:
        client = Mock()
        with pytest.raises(ValidationError, match=r"No poliglot\.yml found"):
            install_local_deps(tmp_path, client)

    def test_rejects_missing_engine_version(self, tmp_path: Path) -> None:
        _write_project_config(tmp_path, engine_version=None)
        client = Mock()
        with pytest.raises(ValidationError, match=r"package\.engineVersion"):
            install_local_deps(tmp_path, client)

    def test_resolves_engine_only_when_no_dependencies(self, tmp_path: Path) -> None:
        _write_project_config(tmp_path, engine_version=">=1 <2")

        client = Mock()
        client.list_compatible_versions.return_value = [
            RegistryVersionRef(version="1.5.0", engine_version=">=1 <2"),
        ]
        client.download_archive.side_effect = _stub_download_to_disk(
            _engine_archive(), checksum="sha256:engine"
        )

        summary = install_local_deps(tmp_path, client)

        assert summary.engine.publisher == "poliglot"
        assert summary.engine.name == "os"
        assert summary.engine.version == "1.5.0"
        assert summary.engine.checksum == "sha256:engine"
        assert summary.dependencies == []
        assert len(summary.fetched) == 1
        assert summary.cached == []

        engine_dir = cache_dir_for(tmp_path, "poliglot", "os", "1.5.0")
        assert engine_dir.is_dir()
        assert (engine_dir / "spec" / "plgt" / "matrix.ttl").is_file()
        assert (engine_dir / ".matrix-installed").read_text() == "sha256:engine"

    def test_resolves_root_dep_and_writes_lockfile(self, tmp_path: Path) -> None:
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        def list_versions(pub, name, engine_version=None):
            if (pub, name) == ("poliglot", "os"):
                return [RegistryVersionRef("1.8.0", ">=1 <2")]
            if (pub, name) == ("widget", "widget"):
                return [RegistryVersionRef("1.2.0", ">=1 <2")]
            msg = f"unexpected list call: {pub}/{name}"
            raise AssertionError(msg)

        def download(pub, name, version, destination: Path) -> str:
            archive = (
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive)
            return f"sha256:{name}-{version}"

        client = Mock()
        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        summary = install_local_deps(tmp_path, client)

        assert summary.engine.version == "1.8.0"
        assert len(summary.dependencies) == 1
        dep = summary.dependencies[0]
        assert (dep.publisher, dep.name, dep.version) == ("widget", "widget", "1.2.0")
        assert dep.root is True

        # Lockfile written and round-trips at the per-mode path (registry-resolve by default).
        lockfile = read_lockfile(lockfile_path_for(tmp_path, workspace=None))
        assert lockfile is not None
        assert lockfile.engine.version == "1.8.0"
        assert lockfile.dependencies[0].version == "1.2.0"

    def test_resolves_transitive_deps_marking_root_false(self, tmp_path: Path) -> None:
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        def list_versions(pub, name, engine_version=None):
            lookup = {
                ("poliglot", "os"): RegistryVersionRef("1.8.0", ">=1 <2"),
                ("widget", "widget"): RegistryVersionRef("1.2.0", ">=1 <2"),
                ("acme", "shared"): RegistryVersionRef("0.3.2", ">=1 <2"),
            }
            return [lookup[(pub, name)]]

        def download(pub, name, version, destination: Path) -> str:
            if (pub, name) == ("poliglot", "os"):
                archive = _engine_archive()
            elif (pub, name) == ("widget", "widget"):
                archive = _user_archive(
                    "widget/widget", transitive_deps={"acme/shared": ">=0 <1"}
                )
            else:
                archive = _user_archive(f"{pub}/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive)
            return f"sha256:{name}-{version}"

        client = Mock()
        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        summary = install_local_deps(tmp_path, client)

        by_name = {(d.publisher, d.name): d for d in summary.dependencies}
        assert by_name[("widget", "widget")].root is True
        assert by_name[("widget", "widget")].via is None
        assert by_name[("acme", "shared")].root is False
        assert by_name[("acme", "shared")].via == "widget/widget"

    def test_detects_transitive_version_conflict(self, tmp_path: Path) -> None:
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"acme/shared": ">=0.4 <1", "widget/widget": ">=1 <2"},
        )

        def list_versions(pub, name, engine_version=None):
            # Both deps want acme/shared but resolve to different versions.
            if (pub, name) == ("poliglot", "os"):
                return [RegistryVersionRef("1.8.0", ">=1 <2")]
            if (pub, name) == ("widget", "widget"):
                return [RegistryVersionRef("1.2.0", ">=1 <2")]
            if (pub, name) == ("acme", "shared"):
                # Pretend latest within the requesting context differs from
                # what widget/widget resolves to. We achieve this by returning
                # only one version, so both root + transitive land on the same
                # version. To simulate divergence here, return separate values
                # depending on the call context — Mock side_effect doesn't
                # expose context, so this test instead verifies the resolver
                # raises when fed the divergence directly via the second call.
                return [RegistryVersionRef("0.4.0", ">=1 <2")]
            raise AssertionError(pub)

        # Force a divergence by having the parent widget/widget declare a
        # transitive on the same package coord but at a different version.
        def download(pub, name, version, destination: Path) -> str:
            if (pub, name) == ("poliglot", "os"):
                archive = _engine_archive()
            elif (pub, name) == ("widget", "widget"):
                # widget declares acme/shared at ">=0 <0.4" which would
                # require 0.3.x, but the project's root dep needs >=0.4.
                # Either way, the resolver pinning catches the version
                # mismatch when both paths attempt to record different
                # versions for the same (pub, name) key.
                archive = _user_archive(
                    "widget/widget", transitive_deps={"acme/shared": "<0.4"}
                )
            else:
                archive = _user_archive(f"{pub}/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive)
            return f"sha256:{name}-{version}"

        # Make list_versions return DIFFERENT picks for the same (acme, shared)
        # depending on the requested range. We approximate by tracking call count.
        seen = {"shared_calls": 0}
        original = list_versions

        def context_list_versions(pub, name, engine_version=None):
            if (pub, name) == ("acme", "shared"):
                seen["shared_calls"] += 1
                if seen["shared_calls"] == 1:
                    return [RegistryVersionRef("0.5.0", ">=1 <2")]  # root pick
                return [RegistryVersionRef("0.3.0", ">=1 <2")]  # transitive pick
            return original(pub, name, engine_version)

        client = Mock()
        client.list_compatible_versions.side_effect = context_list_versions
        client.download_archive.side_effect = download

        with pytest.raises(ServiceError, match="Conflicting versions"):
            install_local_deps(tmp_path, client)

    def test_promotes_transitive_to_root_when_also_declared(
        self, tmp_path: Path
    ) -> None:
        """If widget/widget (declared as a root dep) transitively pulls in
        acme/shared, AND acme/shared is ALSO declared at the project
        root, the second-listed root must end up with root=True (not be
        demoted to 'via widget/widget' because dict ordering happened to
        resolve the transitive first).
        """
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            # Order matters here: widget/widget is listed first and will be
            # resolved first; its transitive dep pulls in acme/shared
            # BEFORE the root-level acme/shared entry is processed.
            dependencies={
                "widget/widget": ">=1 <2",
                "acme/shared": ">=0 <1",
            },
        )

        def list_versions(pub, name, engine_version=None):
            lookup = {
                ("poliglot", "os"): RegistryVersionRef("1.8.0", ">=1 <2"),
                ("widget", "widget"): RegistryVersionRef("1.2.0", ">=1 <2"),
                ("acme", "shared"): RegistryVersionRef("0.3.2", ">=1 <2"),
            }
            return [lookup[(pub, name)]]

        def download(pub, name, version, destination: Path) -> str:
            if (pub, name) == ("poliglot", "os"):
                archive = _engine_archive()
            elif (pub, name) == ("widget", "widget"):
                archive = _user_archive(
                    "widget/widget",
                    transitive_deps={"acme/shared": ">=0 <1"},
                )
            else:
                archive = _user_archive(f"{pub}/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive)
            return f"sha256:{name}-{version}"

        client = Mock()
        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        summary = install_local_deps(tmp_path, client)

        by_coord = {(d.publisher, d.name): d for d in summary.dependencies}
        # acme/shared should end up as a root dep with via=None, not
        # demoted because the transitive walk happened to see it first.
        acme = by_coord[("acme", "shared")]
        assert acme.root is True
        assert acme.via is None

    def test_transient_dep_installs_without_yml_write(self, tmp_path: Path) -> None:
        """`plgt install foo/bar --no-save` injects a transient root dep so
        the package is actually fetched, while leaving poliglot.yml's
        `dependencies:` block alone.
        """
        _write_project_config(tmp_path, engine_version=">=1 <2")

        def list_versions(pub, name, engine_version=None):
            lookup = {
                ("poliglot", "os"): RegistryVersionRef("1.8.0", ">=1 <2"),
                ("acme", "tool"): RegistryVersionRef("1.0.0", ">=1 <2"),
            }
            return [lookup[(pub, name)]]

        def download(pub, name, version, destination: Path) -> str:
            archive = (
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive)
            return f"sha256:{name}-{version}"

        client = Mock()
        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        summary = install_local_deps(
            tmp_path,
            client,
            transient_deps=[
                PackageDependency(publisher="acme", name="tool", version_range=">=1 <2")
            ],
        )

        by_coord = {(d.publisher, d.name): d for d in summary.dependencies}
        assert ("acme", "tool") in by_coord
        assert by_coord[("acme", "tool")].root is True

        # poliglot.yml unchanged: no `dependencies:` was added.
        import yaml as _yaml

        with (tmp_path / "poliglot.yml").open() as f:
            cfg = _yaml.safe_load(f)
        assert "dependencies" not in cfg or not cfg.get("dependencies")

    def test_skips_already_cached_package_on_rerun(self, tmp_path: Path) -> None:
        _write_project_config(tmp_path, engine_version=">=1 <2")
        client = Mock()
        client.list_compatible_versions.return_value = [
            RegistryVersionRef("1.5.0", ">=1 <2")
        ]
        client.download_archive.side_effect = _stub_download_to_disk(
            _engine_archive(), checksum="sha256:engine"
        )

        install_local_deps(tmp_path, client)
        # Second run: marker present, no download
        client.download_archive.reset_mock()
        summary = install_local_deps(tmp_path, client)
        client.download_archive.assert_not_called()
        assert summary.fetched == []
        assert len(summary.cached) == 1


class TestSafeExtract:
    """Tar-bomb defenses on _safe_extract. The caps protect against malicious
    archives served by a hypothetically-compromised registry.
    """

    @staticmethod
    def _archive_with_files(payloads: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in payloads.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def test_rejects_archive_exceeding_byte_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the cap small so we can build a real archive cheaply and still trip it. The
        # cap is read at call time in _safe_extract, so monkeypatching the module attribute
        # works without restarting the import.
        monkeypatch.setattr("plgt.services.deps_install_service.MAX_EXTRACT_BYTES", 64)
        archive = self._archive_with_files({"big.bin": b"X" * 128})
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            with pytest.raises(ServiceError, match="uncompressed size exceeds"):
                _safe_extract(tar, tmp_path)

    def test_rejects_archive_exceeding_member_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the member cap small so we can demonstrate the bound without producing 50k+
        # tar headers.
        monkeypatch.setattr("plgt.services.deps_install_service.MAX_EXTRACT_MEMBERS", 3)
        payloads = {f"f{i}.txt": b"" for i in range(4)}
        archive = self._archive_with_files(payloads)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            with pytest.raises(ServiceError, match="member cap"):
                _safe_extract(tar, tmp_path)

    def test_accepts_archive_under_caps(self, tmp_path: Path) -> None:
        # A normal little archive extracts cleanly.
        archive = self._archive_with_files(
            {"a.txt": b"hello", "subdir/b.txt": b"world"}
        )
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            _safe_extract(tar, tmp_path)
        assert (tmp_path / "a.txt").read_bytes() == b"hello"
        assert (tmp_path / "subdir" / "b.txt").read_bytes() == b"world"


class TestPerModeCacheLayout:
    """Per-mode cache subtrees and lockfile paths isolate workspace-sync
    from registry-resolve so the two can coexist in one repo.
    """

    def test_registry_mode_uses_underscore_registry_subtree(
        self, tmp_path: Path
    ) -> None:
        from plgt.services.deps_install_service import (
            REGISTRY_MODE_SUBDIR,
            cache_root_for,
            lockfile_path_for,
        )

        root = cache_root_for(tmp_path, workspace=None)
        assert root == tmp_path / ".matrix" / "deps" / REGISTRY_MODE_SUBDIR

        lock = lockfile_path_for(tmp_path, workspace=None)
        assert lock == tmp_path / ".matrix" / "deps" / f"{REGISTRY_MODE_SUBDIR}.lock"

    def test_workspace_mode_uses_named_subtree(self, tmp_path: Path) -> None:
        from plgt.services.deps_install_service import (
            cache_root_for,
            lockfile_path_for,
        )

        root = cache_root_for(tmp_path, workspace="dev")
        assert root == tmp_path / ".matrix" / "deps" / "dev"

        lock = lockfile_path_for(tmp_path, workspace="dev")
        assert lock == tmp_path / ".matrix" / "deps" / "dev.lock"

    def test_cache_dir_for_routes_through_per_mode_root(self, tmp_path: Path) -> None:
        assert (
            cache_dir_for(tmp_path, "widget", "widget", "1.5.0", workspace=None)
            == tmp_path
            / ".matrix"
            / "deps"
            / "_registry"
            / "widget"
            / "widget"
            / "1.5.0"
        )
        assert (
            cache_dir_for(tmp_path, "widget", "widget", "1.5.0", workspace="prod")
            == tmp_path / ".matrix" / "deps" / "prod" / "widget" / "widget" / "1.5.0"
        )


class TestWorkspaceSyncResolution:
    """End-to-end install_local_deps in workspace-sync mode. The resolver
    consults the workspace's installed-package list before falling back to
    the registry; local-build-only matches hard-fail.
    """

    @staticmethod
    def _make_workspace_client(installed: list[dict]) -> Mock:
        """Stub a WorkspacePackagesClient whose ``list_installed`` returns
        the supplied rows wrapped in ``InstalledPackageRef``s."""
        from plgt.clients.workspace_packages_client import InstalledPackageRef

        refs = [
            InstalledPackageRef(
                name=row["name"],
                current_version=row["currentVersion"],
                registry_publisher=row.get("registryPublisher"),
                registry_name=row.get("registryName"),
            )
            for row in installed
        ]
        client = Mock()
        client.list_installed.return_value = refs
        return client

    def test_pins_to_workspace_version_when_registry_coord_match(
        self, tmp_path: Path
    ) -> None:
        from plgt.services.deps_install_service import lockfile_path_for
        from plgt.services.deps_lockfile import (
            ORIGIN_REGISTRY_FALLBACK,
            ORIGIN_WORKSPACE_PINNED,
            read_lockfile,
        )

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        # Workspace has widget/widget @ 1.3.5 (older minor than the registry's latest).
        ws_client = self._make_workspace_client(
            [
                {
                    "name": "os",
                    "currentVersion": "1.8.0",
                    "registryPublisher": "poliglot",
                    "registryName": "os",
                },
                {
                    "name": "widget",
                    "currentVersion": "1.3.5",
                    "registryPublisher": "widget",
                    "registryName": "widget",
                },
            ]
        )

        client = Mock()

        # Registry returns a NEWER version, but the workspace wins. We assert that
        # list_compatible_versions is NEVER called for either coord, because both are
        # pinned by the workspace state.
        client.list_compatible_versions.side_effect = AssertionError(
            "registry must not be queried when the workspace pins both deps"
        )

        def download(pub, name, version, destination: Path) -> str:
            # Confirm the resolver asked for the workspace's pinned versions, not the
            # registry's latest.
            assert version in {"1.8.0", "1.3.5"}
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (pub, name) == ("poliglot", "os"):
                destination.write_bytes(_engine_archive())
            else:
                destination.write_bytes(_user_archive(f"{pub}/{name}"))
            return f"sha256:{name}-{version}"

        client.download_archive.side_effect = download

        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client,
        )

        assert summary.engine.version == "1.8.0"
        assert summary.engine.origin == ORIGIN_WORKSPACE_PINNED
        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "1.3.5"
        assert widget.origin == ORIGIN_WORKSPACE_PINNED

        # Lockfile lands under the workspace-named path with the workspace-pinned origins.
        lockfile = read_lockfile(lockfile_path_for(tmp_path, workspace="dev"))
        assert lockfile is not None
        assert lockfile.engine.origin == ORIGIN_WORKSPACE_PINNED
        assert all(
            d.origin in (ORIGIN_WORKSPACE_PINNED, ORIGIN_REGISTRY_FALLBACK)
            for d in lockfile.dependencies
        )

    def test_falls_back_to_registry_and_notifies_on_workspace_gap(
        self, tmp_path: Path
    ) -> None:
        from plgt.services.deps_lockfile import (
            ORIGIN_REGISTRY_FALLBACK,
            ORIGIN_WORKSPACE_PINNED,
        )

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        # Workspace has only the engine; widget/widget is declared but not installed there.
        ws_client = self._make_workspace_client(
            [
                {
                    "name": "os",
                    "currentVersion": "1.8.0",
                    "registryPublisher": "poliglot",
                    "registryName": "os",
                },
            ]
        )

        client = Mock()
        client.list_compatible_versions.side_effect = (
            lambda pub, name, engine_version=None: [
                RegistryVersionRef("1.5.0", ">=1 <2")
            ]
        )

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.download_archive.side_effect = download

        drift_calls: list[tuple[str, str, str]] = []

        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client,
            workspace_drift_notifier=lambda p, n, r: drift_calls.append((p, n, r)),
        )

        assert summary.engine.origin == ORIGIN_WORKSPACE_PINNED
        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "1.5.0"
        assert widget.origin == ORIGIN_REGISTRY_FALLBACK

        assert drift_calls == [("widget", "widget", ">=1 <2")]

    def test_local_build_with_name_collision_does_not_block_registry_resolution(
        self, tmp_path: Path
    ) -> None:
        """Local-build uploads have no publisher metadata, so a name collision
        with a declared registry coord cannot be assumed to be the same
        package. The resolver must NOT hard-fail on name match alone; that
        would block legitimate registry resolution. Local-builds are
        invisible to dep resolution.
        """
        from plgt.services.deps_lockfile import ORIGIN_REGISTRY_FALLBACK

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"my-org/widget": ">=0.1 <1"},
        )

        ws_client = self._make_workspace_client(
            [
                {
                    "name": "os",
                    "currentVersion": "1.8.0",
                    "registryPublisher": "poliglot",
                    "registryName": "os",
                },
                # Local-build whose package name happens to match the declared dep's name.
                # Without publisher metadata, this is NOT a signal that they're the same
                # package.
                {
                    "name": "widget",
                    "currentVersion": "0.1.0+local-abc",
                    "registryPublisher": None,
                    "registryName": None,
                },
            ]
        )

        client = Mock()
        client.list_compatible_versions.side_effect = (
            lambda pub, name, engine_version=None: (
                [RegistryVersionRef("0.3.0", ">=1 <2")]
            )
        )

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.download_archive.side_effect = download

        drift_calls: list[tuple[str, str, str]] = []

        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client,
            workspace_drift_notifier=lambda p, n, r: drift_calls.append((p, n, r)),
        )

        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "0.3.0"
        assert widget.origin == ORIGIN_REGISTRY_FALLBACK
        # The drift notifier fired because the workspace did not have my-org/widget
        # (registry-installed), exactly the normal W0902 surface.
        assert drift_calls == [("my-org", "widget", ">=0.1 <1")]

    def test_workspace_version_out_of_range_calls_mismatch_notifier(
        self, tmp_path: Path
    ) -> None:
        """When the workspace's pinned version doesn't satisfy the declared
        range, the workspace still wins (it's the deployment target) but
        the mismatch is surfaced as PLGT_W0903.
        """
        from plgt.services.deps_lockfile import ORIGIN_WORKSPACE_PINNED

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=2 <3"},  # yml asks for 2.x
        )

        # Workspace has 1.5.0 (out of yml range).
        ws_client = self._make_workspace_client(
            [
                {
                    "name": "os",
                    "currentVersion": "1.8.0",
                    "registryPublisher": "poliglot",
                    "registryName": "os",
                },
                {
                    "name": "widget",
                    "currentVersion": "1.5.0",
                    "registryPublisher": "widget",
                    "registryName": "widget",
                },
            ]
        )

        client = Mock()
        client.download_archive.side_effect = _stub_download_to_disk(
            _engine_archive(), checksum="sha256:engine"
        )

        mismatch_calls: list[tuple[str, str, str, str]] = []

        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client,
            workspace_range_mismatch_notifier=lambda p, n, v, r: mismatch_calls.append(
                (p, n, v, r)
            ),
        )

        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "1.5.0"  # workspace wins despite range mismatch
        assert widget.origin == ORIGIN_WORKSPACE_PINNED

        assert mismatch_calls == [("widget", "widget", "1.5.0", ">=2 <3")]

    def test_requires_workspace_packages_client_in_workspace_mode(
        self, tmp_path: Path
    ) -> None:
        _write_project_config(tmp_path, engine_version=">=1 <2")
        client = Mock()
        with pytest.raises(ServiceError, match="WorkspacePackagesClient"):
            install_local_deps(tmp_path, client, workspace="dev")


class TestLockfilePinHonor:
    """The lockfile is authoritative on subsequent installs: prior pins
    short-circuit source-of-truth lookups. Pins that no longer satisfy
    the declared range trigger PLGT_W0901 and a re-resolve.
    """

    def test_subsequent_install_honors_lockfile_pin(self, tmp_path: Path) -> None:
        """First install resolves at the registry's then-latest; a second
        install when the registry has bumped to a newer version must still
        return the pinned version from the lockfile.
        """
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        client = Mock()
        registry_state = {"widget": "1.2.0", "os": "1.8.0"}
        list_calls: list[tuple[str, str]] = []

        def list_versions(pub, name, engine_version=None):
            list_calls.append((pub, name))
            ver = registry_state[name]
            return [RegistryVersionRef(ver, ">=1 <2")]

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        # First install: no lockfile, full resolution to the registry's latest.
        first = install_local_deps(tmp_path, client)
        assert first.dependencies[0].version == "1.2.0"

        # Registry's "latest" advances; without pin honor we'd see 1.5.0 on the next install.
        registry_state["widget"] = "1.5.0"
        list_calls.clear()

        # Second install: lockfile exists with 1.2.0 pinned. The resolver should NOT call
        # list_compatible_versions for `widget/widget` because the pin satisfies the range.
        second = install_local_deps(tmp_path, client)
        assert (
            second.dependencies[0].version == "1.2.0"
        )  # pin held, did NOT advance to 1.5.0
        assert ("widget", "widget") not in list_calls
        assert ("poliglot", "os") not in list_calls

    def test_drift_triggers_w0901_and_reresolve(self, tmp_path: Path) -> None:
        """Yml range changed since last install (lockfile pin no longer
        satisfies). Notifier fires; resolver re-queries the registry.
        """
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        client = Mock()

        def list_versions(pub, name, engine_version=None):
            if (pub, name) == ("poliglot", "os"):
                return [RegistryVersionRef("1.8.0", ">=1 <2")]
            return [RegistryVersionRef("1.5.0", ">=1 <2")]

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        # First install pins 1.5.0.
        first = install_local_deps(tmp_path, client)
        assert first.dependencies[0].version == "1.5.0"

        # Bump the declared range in poliglot.yml so 1.5.0 no longer satisfies.
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=2 <3"},  # 1.5.0 no longer satisfies
        )
        # Provide a 2.x version in the registry for the re-resolution to find.
        client.list_compatible_versions.side_effect = (
            lambda pub, name, engine_version=None: (
                [RegistryVersionRef("1.8.0", ">=1 <2")]
                if name == "os"
                else [RegistryVersionRef("2.0.0", ">=1 <2")]
            )
        )

        drift_calls: list[tuple[str, str, str, str]] = []
        summary = install_local_deps(
            tmp_path,
            client,
            lockfile_drift_notifier=lambda p, n, pv, r: drift_calls.append(
                (p, n, pv, r)
            ),
        )

        assert summary.dependencies[0].version == "2.0.0"
        assert drift_calls == [("widget", "widget", "1.5.0", ">=2 <3")]

    def test_pin_vs_workspace_notifier_fires_on_silent_divergence(
        self, tmp_path: Path
    ) -> None:
        """In workspace-sync mode, when the lockfile pin still satisfies the
        range but the workspace has a different (also in-range) version,
        surface the silent divergence as PLGT_W0904. The pin still wins
        (lockfile authority), but the author needs to know validation will
        not match what gets deployed.
        """
        from plgt.clients.workspace_packages_client import InstalledPackageRef

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        client = Mock()

        def list_versions(pub, name, engine_version=None):
            if (pub, name) == ("poliglot", "os"):
                return [RegistryVersionRef("1.8.0", ">=1 <2")]
            return [RegistryVersionRef("1.2.0", ">=1 <2")]

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        # First install (workspace mode) pins widget@1.2.0 (registry-only setup; workspace
        # didn't have it, so the pin came from registry fallback).
        ws_client_empty = Mock()
        ws_client_empty.list_installed.return_value = [
            InstalledPackageRef(
                name="os",
                current_version="1.8.0",
                registry_publisher="poliglot",
                registry_name="os",
            )
        ]
        install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client_empty,
        )

        # Workspace now has widget@1.4.0 — different version, still in range.
        ws_client_drifted = Mock()
        ws_client_drifted.list_installed.return_value = [
            InstalledPackageRef(
                name="os",
                current_version="1.8.0",
                registry_publisher="poliglot",
                registry_name="os",
            ),
            InstalledPackageRef(
                name="widget",
                current_version="1.4.0",
                registry_publisher="widget",
                registry_name="widget",
            ),
        ]

        pin_vs_ws_calls: list[tuple[str, str, str, str]] = []
        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client_drifted,
            pin_vs_workspace_notifier=lambda p, n, pv, wv: pin_vs_ws_calls.append(
                (p, n, pv, wv)
            ),
        )

        # Pin held: widget is still 1.2.0 in the summary, NOT bumped to 1.4.0.
        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "1.2.0"

        # Notifier fired with the divergence.
        assert pin_vs_ws_calls == [("widget", "widget", "1.2.0", "1.4.0")]

    def test_update_flag_bypasses_pin_honor(self, tmp_path: Path) -> None:
        """``plgt install --update`` should re-resolve every coord even when
        the lockfile pin still satisfies. Modeled as the resolver being
        constructed with ``force_refetch=True``.
        """
        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        client = Mock()
        list_calls: list[tuple[str, str]] = []

        def list_versions(pub, name, engine_version=None):
            list_calls.append((pub, name))
            if (pub, name) == ("poliglot", "os"):
                return [RegistryVersionRef("1.8.0", ">=1 <2")]
            return [RegistryVersionRef("1.5.0", ">=1 <2")]

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                _engine_archive() if name == "os" else _user_archive(f"{pub}/{name}")
            )
            return f"sha256:{name}-{version}"

        client.list_compatible_versions.side_effect = list_versions
        client.download_archive.side_effect = download

        install_local_deps(tmp_path, client)
        list_calls.clear()

        # --update path should re-query everything.
        install_local_deps(tmp_path, client, update=True)
        assert ("widget", "widget") in list_calls
        assert ("poliglot", "os") in list_calls


class TestTransitiveWorkspaceSync:
    """Workspace pins apply to transitive deps too: a workspace-pinned
    root that declares a transitive dep also installed on the workspace
    must pin the transitive from the workspace, not re-resolve via the
    registry.
    """

    @staticmethod
    def _make_workspace_client(installed: list[dict]) -> Mock:
        from plgt.clients.workspace_packages_client import InstalledPackageRef

        refs = [
            InstalledPackageRef(
                name=row["name"],
                current_version=row["currentVersion"],
                registry_publisher=row.get("registryPublisher"),
                registry_name=row.get("registryName"),
            )
            for row in installed
        ]
        client = Mock()
        client.list_installed.return_value = refs
        return client

    def test_transitive_pinned_from_workspace_when_installed(
        self, tmp_path: Path
    ) -> None:
        from plgt.services.deps_lockfile import ORIGIN_WORKSPACE_PINNED

        _write_project_config(
            tmp_path,
            engine_version=">=1 <2",
            dependencies={"widget/widget": ">=1 <2"},
        )

        # Workspace has both widget/widget and its transitive acme/shared installed.
        ws_client = self._make_workspace_client(
            [
                {
                    "name": "os",
                    "currentVersion": "1.8.0",
                    "registryPublisher": "poliglot",
                    "registryName": "os",
                },
                {
                    "name": "widget",
                    "currentVersion": "1.2.0",
                    "registryPublisher": "widget",
                    "registryName": "widget",
                },
                {
                    "name": "shared",
                    "currentVersion": "0.5.0",
                    "registryPublisher": "acme",
                    "registryName": "shared",
                },
            ]
        )

        client = Mock()
        # Registry should not be queried for any of the three coords (all workspace-pinned).
        client.list_compatible_versions.side_effect = AssertionError(
            "registry must not be queried when workspace pins all coords transitively"
        )

        def download(pub, name, version, destination: Path) -> str:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (pub, name) == ("poliglot", "os"):
                destination.write_bytes(_engine_archive())
            elif (pub, name) == ("widget", "widget"):
                destination.write_bytes(
                    _user_archive(
                        "widget/widget", transitive_deps={"acme/shared": ">=0.5 <1"}
                    )
                )
            else:
                destination.write_bytes(_user_archive(f"{pub}/{name}"))
            return f"sha256:{name}-{version}"

        client.download_archive.side_effect = download

        summary = install_local_deps(
            tmp_path,
            client,
            workspace="dev",
            workspace_packages_client=ws_client,
        )

        # Root and transitive both pinned to the workspace's versions.
        widget = next(d for d in summary.dependencies if d.name == "widget")
        assert widget.version == "1.2.0"
        assert widget.origin == ORIGIN_WORKSPACE_PINNED
        shared = next(d for d in summary.dependencies if d.name == "shared")
        assert shared.version == "0.5.0"
        assert shared.origin == ORIGIN_WORKSPACE_PINNED
