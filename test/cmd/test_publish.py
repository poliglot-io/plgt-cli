"""Unit tests for the ``plgt publish / yank / unyank`` typer commands.

The CLI's :mod:`plgt.cmd.publish` orchestrates session resolution, local build,
publisher slug discovery, and the publish/yank HTTP call. The HTTP client itself
is covered by ``test_publish_client.py``; these tests verify the command-level
glue — flag parsing, auth gating, agreement gating, dry-run behavior, error
mapping, and the ``<name>@<version>`` parser for yank/unyank.
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from plgt.cmd.publish import app
from plgt.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ResourceNotFoundError,
    ValidationError,
)
from plgt.models.build_types import PackageBuildResult, PackageConfig
from typer.testing import CliRunner

runner = CliRunner()


def _build_config(tmp_path: Path) -> PackageConfig:
    return PackageConfig(
        name="crm",
        version="0.0.1",
        engine_version=">=1 <2",
        project_dir=tmp_path,
        matrices=[],
        dependencies=[],
    )


def _build_result(tarball: Path) -> PackageBuildResult:
    return PackageBuildResult(
        package_file=tarball,
        package_name="crm",
        package_version="0.0.1",
        matrices=[],
    )


@pytest.fixture
def tarball(tmp_path: Path) -> Path:
    """Concrete tarball-shaped file on disk; the command stats it for size."""
    path = tmp_path / "crm-0.0.1.tgz"
    path.write_bytes(b"gzipped-fake-bytes")
    return path


@pytest.fixture
def authed_session() -> Mock:
    session = Mock()
    session.authenticated = True
    return session


def _patch_build(tmp_path: Path, tarball: Path):
    """Common patches for build_config + build_workflow that publish() runs."""
    config = _build_config(tmp_path)
    result = _build_result(tarball)
    return (
        patch("plgt.cmd.publish.create_build_config", return_value=config),
        patch("plgt.cmd.publish.execute_build_workflow", return_value=result),
        patch("plgt.cmd.publish.create_progress_tracker", return_value=MagicMock()),
    )


class TestPublish:
    """Tests for ``plgt publish``."""

    def test_exits_when_unauthenticated(self, tmp_path: Path, tarball: Path) -> None:
        """No session → user told to run `plgt auth login`, exit 1."""
        unauthed = Mock()
        unauthed.authenticated = False
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=unauthed),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout
        assert "plgt auth login" in result.stdout

    def test_exits_when_agreement_not_accepted(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """Authed session + publisher without acceptance → fail with link to settings."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        client = Mock()
        client.get_my_publisher.return_value = {
            "slug": "alice",
            "publisherAgreementAcceptedAt": None,
        }
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 1
        assert "Publisher Agreement not accepted" in result.stdout
        # Critical: we must not call publish() before the agreement check.
        client.publish.assert_not_called()

    def test_dry_run_invokes_client_with_dry_run_true(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """--dry-run forwards dry_run=True and prints the dry-run success line."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        client = Mock()
        client.get_my_publisher.return_value = {
            "slug": "alice",
            "publisherAgreementAcceptedAt": "2026-04-01T00:00:00Z",
        }
        client.publish.return_value = None
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish", "--dry-run"])
        assert result.exit_code == 0
        client.publish.assert_called_once()
        kwargs = client.publish.call_args.kwargs
        assert kwargs["dry_run"] is True
        assert "Dry-run OK" in result.stdout

    def test_happy_path_prints_published_line(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """Real publish prints the publisher/name/version success line."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        client = Mock()
        client.get_my_publisher.return_value = {
            "slug": "alice",
            "publisherAgreementAcceptedAt": "2026-04-01T00:00:00Z",
        }
        client.publish.return_value = {"version": "0.0.1"}
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 0
        assert "Published alice/crm v0.0.1" in result.stdout
        kwargs = client.publish.call_args.kwargs
        assert kwargs["dry_run"] is False

    def test_validation_error_renders_report_and_exits_2(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """422 → ValidationError carries .report; we render it and exit 2 (distinct
        from generic 1 so CI can branch on "fix your bundle" vs "infra wobble")."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        err = ValidationError("Validation failed")
        err.report = {
            "violations": [
                {
                    "rule": "BoundaryRule.namespace_must_be_emitted",
                    "message": "Declared namespace not emitted by any TTL",
                    "suggestion": "Define at least one resource in the namespace",
                }
            ],
            "warnings": [],
        }
        client = Mock()
        client.get_my_publisher.return_value = {
            "slug": "alice",
            "publisherAgreementAcceptedAt": "2026-04-01T00:00:00Z",
        }
        client.publish.side_effect = err
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 2
        assert "Validation failed" in result.stdout
        # Rich wraps long table cells; assert on substrings that survive wrapping
        # rather than the full message text.
        assert "BoundaryRule" in result.stdout
        assert "Declared namespace not" in result.stdout
        assert "any TTL" in result.stdout

    def test_conflict_error_exits_1(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """409 from publish-client (e.g., version-already-published) → exit 1, no traceback."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        client = Mock()
        client.get_my_publisher.return_value = {
            "slug": "alice",
            "publisherAgreementAcceptedAt": "2026-04-01T00:00:00Z",
        }
        client.publish.side_effect = ConflictError("Version already published")
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 1
        assert "Conflict" in result.stdout
        assert "Version already published" in result.stdout

    def test_get_my_publisher_failure_exits_before_publish(
        self, tmp_path: Path, tarball: Path, authed_session: Mock
    ) -> None:
        """If /publishers/me fails, we never attempt the publish call."""
        cfg_patch, build_patch, progress_patch = _patch_build(tmp_path, tarball)
        client = Mock()
        client.get_my_publisher.side_effect = ResourceNotFoundError(
            "No publisher associated with your account"
        )
        with (
            cfg_patch,
            build_patch,
            progress_patch,
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["publish"])
        assert result.exit_code == 1
        client.publish.assert_not_called()


class TestYankUnyank:
    """Tests for ``plgt yank`` and ``plgt unyank`` — argument parsing + happy path."""

    def test_yank_rejects_bad_reference(self, authed_session: Mock) -> None:
        """``plgt yank not-a-ref --reason "..."`` exits 2 with a clear parser message,
        before any network call."""
        client = Mock()
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["yank", "not-a-ref", "--reason", "buggy"])
        assert result.exit_code == 2
        assert "Invalid package reference" in result.stdout
        client.yank.assert_not_called()

    def test_yank_requires_reason(self, authed_session: Mock) -> None:
        """Missing --reason is a typer-level error; we never reach the client."""
        client = Mock()
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["yank", "crm@0.0.1"])
        assert result.exit_code != 0
        client.yank.assert_not_called()

    def test_yank_happy_path(self, authed_session: Mock) -> None:
        """Valid ref + reason → calls client.yank with parsed name/version and the reason."""
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.yank.return_value = {"yankReason": "wrong dep version"}
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(
                app, ["yank", "crm@0.0.1", "--reason", "wrong dep version"]
            )
        assert result.exit_code == 0
        client.yank.assert_called_once_with(
            "alice", "crm", "0.0.1", "wrong dep version", force=False
        )
        assert "Yanked alice/crm@0.0.1" in result.stdout

    def test_yank_with_force_flag(self, authed_session: Mock) -> None:
        """--force threads through to client.yank so the backend force gate is opt-in."""
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.yank.return_value = {"yankReason": "retiring"}
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(
                app, ["yank", "crm@0.0.1", "--reason", "retiring", "--force"]
            )
        assert result.exit_code == 0
        client.yank.assert_called_once_with(
            "alice", "crm", "0.0.1", "retiring", force=True
        )

    def test_unyank_happy_path(self, authed_session: Mock) -> None:
        """Valid ref → calls client.unyank with parsed name/version."""
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.unyank.return_value = None
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["unyank", "crm@0.0.1"])
        assert result.exit_code == 0
        client.unyank.assert_called_once_with("alice", "crm", "0.0.1")
        assert "Unyanked alice/crm@0.0.1" in result.stdout

    def test_unyank_admin_yanked_surfaces_conflict(self, authed_session: Mock) -> None:
        """Server-side admin-yanked guard returns 409 → ConflictError → exit 1 with message."""
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.unyank.side_effect = ConflictError(
            "Cannot unyank admin-yanked version (yankedByAdmin=true)"
        )
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["unyank", "crm@0.0.1"])
        assert result.exit_code == 1
        assert "admin-yanked" in result.stdout


class TestListPackages:
    """`plgt list` — publisher self-service catalog view."""

    def test_list_renders_table_for_packages(self, authed_session: Mock) -> None:
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [
                {
                    "name": "claude",
                    "publisherSlug": "alice",
                    "latestVersion": "1.4.0",
                    "versionCount": 3,
                    "yankedVersionCount": 0,
                    "latestVersionYanked": False,
                    "installCount": 7,
                },
                {
                    "name": "openai",
                    "publisherSlug": "alice",
                    "latestVersion": "0.9.0",
                    "versionCount": 2,
                    "yankedVersionCount": 1,
                    "latestVersionYanked": True,
                    "installCount": 2,
                },
            ],
            "currentPage": 0,
            "totalPages": 1,
            "totalResults": 2,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        # Both package names appear, and the yanked-latest case shows the (yanked) badge.
        assert "alice/claude" in result.stdout
        assert "alice/openai" in result.stdout
        assert "yanked" in result.stdout.lower()

    def test_list_empty_publisher_shows_first_publish_hint(
        self, authed_session: Mock
    ) -> None:
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [],
            "currentPage": 0,
            "totalPages": 0,
            "totalResults": 0,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "No packages published yet" in result.stdout
        assert "plgt publish" in result.stdout

    def test_list_json_outputs_paged_envelope(self, authed_session: Mock) -> None:
        # --json should emit the raw paged shape for scripting (totalResults, currentPage, etc).
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        paged = {
            "items": [
                {
                    "name": "demo",
                    "publisherSlug": "alice",
                    "versionCount": 1,
                }
            ],
            "currentPage": 0,
            "totalPages": 1,
            "totalResults": 1,
        }
        client.list_my_packages.return_value = paged
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0
        assert "demo" in result.stdout
        assert "totalResults" in result.stdout

    def test_list_auth_error_surfaces_message(self, authed_session: Mock) -> None:
        client = Mock()
        client.get_my_publisher.side_effect = AuthenticationError("Not signed in")
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "Not signed in" in result.stdout

    def test_list_page_flag_translates_to_zero_indexed_server_call(
        self, authed_session: Mock
    ) -> None:
        # --page is 1-indexed for users; the platform is 0-indexed. Pin the translation so a
        # future refactor can't silently introduce an off-by-one in either direction.
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [
                {
                    "name": "demo",
                    "publisherSlug": "alice",
                    "latestVersion": "1.0.0",
                    "versionCount": 1,
                    "yankedVersionCount": 0,
                    "latestVersionYanked": False,
                    "installCount": 0,
                }
            ],
            "currentPage": 2,
            "totalPages": 5,
            "totalResults": 120,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list", "--page", "3", "--limit", "25"])
        assert result.exit_code == 0
        client.list_my_packages.assert_called_once_with(page=2, size=25)

    def test_list_shows_next_page_hint_when_more_pages_exist(
        self, authed_session: Mock
    ) -> None:
        # The footer should prompt the user to fetch the next page when they're not on the last.
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [
                {
                    "name": "demo",
                    "publisherSlug": "alice",
                    "latestVersion": "1.0.0",
                    "versionCount": 1,
                    "yankedVersionCount": 0,
                    "latestVersionYanked": False,
                    "installCount": 0,
                }
            ],
            "currentPage": 0,
            "totalPages": 3,
            "totalResults": 51,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "page 1 of 3" in result.stdout
        assert "plgt list --page 2" in result.stdout

    def test_list_on_last_page_does_not_suggest_next_page(
        self, authed_session: Mock
    ) -> None:
        # Don't dangle a "next page" hint when there is no next page.
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [
                {
                    "name": "demo",
                    "publisherSlug": "alice",
                    "latestVersion": "1.0.0",
                    "versionCount": 1,
                    "yankedVersionCount": 0,
                    "latestVersionYanked": False,
                    "installCount": 0,
                }
            ],
            "currentPage": 2,
            "totalPages": 3,
            "totalResults": 51,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list", "--page", "3"])
        assert result.exit_code == 0
        assert "last page" in result.stdout
        assert "plgt list --page 4" not in result.stdout

    def test_list_empty_page_above_one_suggests_first_page(
        self, authed_session: Mock
    ) -> None:
        # If the user lands on a page beyond the data (e.g. a stale link), the empty-state hint
        # should not say "publish your first" — it should suggest they go back to page 1.
        client = Mock()
        client.get_my_publisher.return_value = {"slug": "alice"}
        client.list_my_packages.return_value = {
            "items": [],
            "currentPage": 4,
            "totalPages": 3,
            "totalResults": 50,
        }
        with (
            patch("plgt.cmd.publish.config.get_session", return_value=authed_session),
            patch("plgt.cmd.publish.PublishClient", return_value=client),
        ):
            result = runner.invoke(app, ["list", "--page", "5"])
        assert result.exit_code == 0
        assert "plgt list --page 1" in result.stdout
        assert "publish your first" not in result.stdout
