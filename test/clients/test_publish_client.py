"""Unit tests for ``plgt.clients.publish_client``."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from plgt.clients.publish_client import DEFAULT_LIST_PAGE_SIZE, PublishClient
from plgt.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError,
)


def _http_error(
    status_code: int, payload: dict | str = ""
) -> requests.exceptions.HTTPError:
    """Build a fake HTTPError matching what requests would surface on a 4xx response."""
    response = Mock()
    response.status_code = status_code
    if isinstance(payload, dict):
        response.json.return_value = payload
        response.text = ""
    else:
        response.json.side_effect = ValueError("not json")
        response.text = payload
    return requests.exceptions.HTTPError(response=response)


class TestGetMyPublisher:
    def test_returns_data_field(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {"data": {"slug": "alice"}}
        session.get.return_value = response

        client = PublishClient(session)
        assert client.get_my_publisher() == {"slug": "alice"}
        session.get.assert_called_once_with("/api/v1/registry/publishers/me")

    def test_401_maps_to_authentication_error(self) -> None:
        session = Mock()
        session.get.side_effect = _http_error(401)

        client = PublishClient(session)
        with pytest.raises(AuthenticationError, match="plgt auth login"):
            client.get_my_publisher()

    def test_404_maps_to_resource_not_found(self) -> None:
        session = Mock()
        session.get.side_effect = _http_error(404)

        client = PublishClient(session)
        with pytest.raises(ResourceNotFoundError, match="No publisher"):
            client.get_my_publisher()


class TestPublish:
    def test_success_returns_data(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        response = Mock()
        response.status_code = 201
        response.json.return_value = {"data": {"version": "1.0.0"}}
        session.post.return_value = response

        client = PublishClient(session)
        result = client.publish("alice", "test-pkg", tarball)

        assert result == {"version": "1.0.0"}
        # No dryRun param passed on a real publish.
        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        assert kwargs["params"] is None

    def test_dry_run_passes_query_param_and_returns_none(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        response = Mock()
        response.status_code = 200
        # Dry-run server returns 200 with null data
        response.json.return_value = {"data": None}
        session.post.return_value = response

        client = PublishClient(session)
        result = client.publish("alice", "test-pkg", tarball, dry_run=True)

        assert result is None
        _, kwargs = session.post.call_args
        assert kwargs["params"] == {"dryRun": "true"}

    def test_422_maps_to_validation_error_with_report(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        report = {
            "error": "ValidationFailed",
            "violations": [
                {
                    "rule": "publisher-boundary-on-resource",
                    "message": "Resource <foo> outside boundary",
                    "suggestion": "Declare a dependency",
                }
            ],
        }
        session = Mock()
        session.post.side_effect = _http_error(
            422,
            {
                "error": {
                    "code": "ValidationFailed",
                    "message": "Publish validation failed",
                    "details": report,
                }
            },
        )

        client = PublishClient(session)
        with pytest.raises(ValidationError) as excinfo:
            client.publish("alice", "test-pkg", tarball)
        # The structured report is attached so the CLI can render it.
        assert excinfo.value.report == report

    def test_409_maps_to_conflict_error(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        session.post.side_effect = _http_error(
            409, {"error": {"code": "Conflict", "message": "Version exists"}}
        )

        client = PublishClient(session)
        with pytest.raises(ConflictError, match="Version exists"):
            client.publish("alice", "test-pkg", tarball)

    def test_401_maps_to_authentication_error(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        session.post.side_effect = _http_error(401)

        client = PublishClient(session)
        with pytest.raises(AuthenticationError):
            client.publish("alice", "test-pkg", tarball)

    def test_403_maps_to_authentication_error(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        session.post.side_effect = _http_error(
            403,
            {
                "error": {
                    "code": "Forbidden",
                    "message": "Publisher Agreement not accepted",
                }
            },
        )

        client = PublishClient(session)
        with pytest.raises(AuthenticationError, match="Publisher Agreement"):
            client.publish("alice", "test-pkg", tarball)

    def test_429_maps_to_service_error_with_code(self, tmp_path: Path) -> None:
        tarball = tmp_path / "package.tgz"
        tarball.write_bytes(b"fake")
        session = Mock()
        session.post.side_effect = _http_error(
            429, {"error": {"code": "RateLimitExceeded", "message": "Slow down"}}
        )

        client = PublishClient(session)
        with pytest.raises(ServiceError, match="Slow down"):
            client.publish("alice", "test-pkg", tarball)


class TestYankUnyank:
    def test_yank_posts_reason(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {"version": "1.0.0", "yankReason": "broken"}
        }
        session.post.return_value = response

        client = PublishClient(session)
        result = client.yank("alice", "test-pkg", "1.0.0", "broken")

        assert result == {"version": "1.0.0", "yankReason": "broken"}
        session.post.assert_called_once_with(
            "/api/v1/registry/publishers/alice/packages/test-pkg/versions/1.0.0/yank",
            json={"reason": "broken", "force": False},
        )

    def test_yank_with_force_flag(self) -> None:
        # Required when yanking the only live version. The CLI's --force flag threads through
        # to the backend's force=true gate.
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {"version": "1.0.0", "yankReason": "retiring package"}
        }
        session.post.return_value = response

        client = PublishClient(session)
        result = client.yank(
            "alice", "test-pkg", "1.0.0", "retiring package", force=True
        )

        assert result == {"version": "1.0.0", "yankReason": "retiring package"}
        session.post.assert_called_once_with(
            "/api/v1/registry/publishers/alice/packages/test-pkg/versions/1.0.0/yank",
            json={"reason": "retiring package", "force": True},
        )

    def test_list_my_packages_returns_paged_envelope(self) -> None:
        # /me/packages returns ApiResponse<PagedResponse<MyPackageResponse>>. The client unwraps
        # the data envelope and hands back the paged shape so the CLI can read items + counts.
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {
                "items": [
                    {
                        "name": "demo",
                        "publisher": {"slug": "alice"},
                        "latestVersion": "1.0.0",
                        "versionCount": 2,
                        "yankedVersionCount": 1,
                        "latestVersionYanked": False,
                        "installCount": 3,
                    }
                ],
                "currentPage": 0,
                "totalPages": 1,
                "totalResults": 1,
            }
        }
        session.get.return_value = response

        client = PublishClient(session)
        result = client.list_my_packages()

        assert result["totalResults"] == 1
        assert result["items"][0]["name"] == "demo"
        # Reference the constant rather than hardcoding "100" so the test doesn't drift when the
        # default cap changes.
        session.get.assert_called_once_with(
            f"/api/v1/registry/publishers/me/packages?page=0&size={DEFAULT_LIST_PAGE_SIZE}"
        )

    def test_list_my_packages_passes_through_explicit_page_args(self) -> None:
        # Explicit-arg path: caller wants page 2 with a smaller size. URL must reflect both
        # arguments verbatim — pin the contract separately from the default-args test so a
        # signature change to one doesn't mask the other.
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {"items": [], "currentPage": 2, "totalPages": 3, "totalResults": 47}
        }
        session.get.return_value = response

        client = PublishClient(session)
        client.list_my_packages(page=2, size=20)

        session.get.assert_called_once_with(
            "/api/v1/registry/publishers/me/packages?page=2&size=20"
        )

    def test_unyank_posts_no_body(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {"data": {"version": "1.0.0"}}
        session.post.return_value = response

        client = PublishClient(session)
        result = client.unyank("alice", "test-pkg", "1.0.0")

        assert result == {"version": "1.0.0"}
        session.post.assert_called_once_with(
            "/api/v1/registry/publishers/alice/packages/test-pkg/versions/1.0.0/unyank"
        )

    def test_yank_admin_block_maps_to_conflict(self) -> None:
        session = Mock()
        session.post.side_effect = _http_error(
            409,
            {
                "error": {
                    "code": "Conflict",
                    "message": "yanked by an administrator",
                }
            },
        )

        client = PublishClient(session)
        with pytest.raises(ConflictError, match="admin"):
            client.unyank("alice", "test-pkg", "1.0.0")
