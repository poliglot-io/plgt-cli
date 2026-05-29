"""Unit tests for ``plgt.clients.workspace_packages_client``."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from plgt.clients.workspace_packages_client import (
    InstalledPackageRef,
    WorkspacePackagesClient,
)
from plgt.core.exceptions import ResourceNotFoundError, ServiceError


def _envelope(items: list[dict], *, total_pages: int = 1) -> dict:
    return {
        "data": {
            "items": items,
            "currentPage": 1,
            "totalPages": total_pages,
            "totalResults": len(items),
        }
    }


class TestListInstalled:
    def test_returns_registry_and_local_build_rows(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = _envelope(
            [
                {
                    "name": "widget",
                    "currentVersion": "1.5.2",
                    "registryPublisher": "widget",
                    "registryName": "widget",
                },
                {
                    "name": "internal-widget",
                    "currentVersion": "0.1.0+local-abc",
                    "registryPublisher": None,
                    "registryName": None,
                },
            ]
        )
        session.get.return_value = response

        client = WorkspacePackagesClient(session)
        result = client.list_installed("dev")

        assert len(result) == 2
        widget = next(r for r in result if r.name == "widget")
        assert widget.is_registry_installed is True
        assert widget.current_version == "1.5.2"
        assert widget.registry_publisher == "widget"

        widget = next(r for r in result if r.name == "internal-widget")
        assert widget.is_registry_installed is False
        assert widget.registry_publisher is None
        assert widget.registry_name is None

        session.get.assert_called_with(
            "/api/v1/packages/dev",
            params={"page": 1, "pageSize": 100},
        )

    def test_paginates_until_total_pages_reached(self) -> None:
        session = Mock()
        page1 = Mock()
        page1.json.return_value = _envelope(
            [
                {
                    "name": "a",
                    "currentVersion": "1.0.0",
                    "registryPublisher": "x",
                    "registryName": "a",
                }
            ],
            total_pages=2,
        )
        page2 = Mock()
        page2.json.return_value = _envelope(
            [
                {
                    "name": "b",
                    "currentVersion": "2.0.0",
                    "registryPublisher": "x",
                    "registryName": "b",
                }
            ],
            total_pages=2,
        )
        session.get.side_effect = [page1, page2]

        client = WorkspacePackagesClient(session)
        result = client.list_installed("dev")

        assert {r.name for r in result} == {"a", "b"}
        # Two paginated calls
        assert session.get.call_count == 2

    def test_rewraps_404_with_workspace_not_found(self) -> None:
        session = Mock()
        session.get.side_effect = ResourceNotFoundError("404")

        client = WorkspacePackagesClient(session)
        with pytest.raises(ResourceNotFoundError, match="Workspace not found: ghost"):
            client.list_installed("ghost")

    def test_skips_rows_missing_name_or_version(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = _envelope(
            [
                {
                    "name": "ok",
                    "currentVersion": "1.0.0",
                    "registryPublisher": "p",
                    "registryName": "ok",
                },
                {
                    "name": None,
                    "currentVersion": "1.0.0",
                    "registryPublisher": "p",
                    "registryName": "bad",
                },
                {
                    "name": "no-version",
                    "currentVersion": "",
                    "registryPublisher": "p",
                    "registryName": "no-version",
                },
            ]
        )
        session.get.return_value = response

        client = WorkspacePackagesClient(session)
        result = client.list_installed("dev")

        assert [r.name for r in result] == ["ok"]

    def test_installed_package_ref_is_frozen(self) -> None:
        ref = InstalledPackageRef(
            name="x",
            current_version="1.0.0",
            registry_publisher="p",
            registry_name="x",
        )
        # dataclasses.FrozenInstanceError is a subclass of AttributeError; either is
        # acceptable for a frozen dataclass.
        with pytest.raises(AttributeError):
            ref.name = "mutated"  # type: ignore[misc]

    def test_network_error_wraps_as_service_error(self) -> None:
        import requests

        session = Mock()
        session.get.side_effect = requests.exceptions.ConnectionError("boom")

        client = WorkspacePackagesClient(session)
        with pytest.raises(ServiceError, match="Failed to list installed"):
            client.list_installed("dev")
