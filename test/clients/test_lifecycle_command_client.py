"""Unit tests for lifecycle_command_client module.

Tests cover LifecycleCommandClient functionality including listing commands,
getting command status, fetching events, and retrieving validation reports.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from plgt.clients.lifecycle_command_client import LifecycleCommandClient
from plgt.core.exceptions import ResourceNotFoundError, ServiceError
from plgt.models.lifecycle_command import (
    LifecycleCommandStatus,
    LifecycleEventLevel,
)


class TestLifecycleCommandClientInstall:
    """Test installation creation functionality."""

    def test_install_package_success(self):
        """Test successful package deployment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "package.tgz"
            package_path.write_bytes(b"compressed package data")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "dep-123",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "PENDING",
                }
            }
            mock_session.post.return_value = mock_response

            client = LifecycleCommandClient(mock_session)
            result = client.install_package("test-workspace", package_path)

            assert result.command_id == "dep-123"
            assert result.package_name == "test-package"
            assert result.status == "PENDING"
            mock_session.post.assert_called_once()

    def test_install_package_force_update(self):
        """Test installation with force_update flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "package.tgz"
            package_path.write_bytes(b"compressed package data")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "dep-456",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "PENDING",
                }
            }
            mock_session.post.return_value = mock_response

            client = LifecycleCommandClient(mock_session)
            result = client.install_package(
                "test-workspace", package_path, force_update=True
            )

            assert result.command_id == "dep-456"
            # Verify force_update was passed in params
            call_args = mock_session.post.call_args
            assert call_args[1]["params"]["forceUpdate"] == "true"

    def test_install_package_http_error(self):
        """Test installation with HTTP error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "package.tgz"
            package_path.write_bytes(b"compressed package data")

            mock_session = Mock()
            mock_session.post.side_effect = requests.RequestException("Network error")

            client = LifecycleCommandClient(mock_session)

            with pytest.raises(ServiceError, match="Failed to install package"):
                client.install_package("test-workspace", package_path)

    def test_install_package_includes_bindings_payload(self):
        """variableBindings + secretBindings ride alongside the upload as JSON."""
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "package.tgz"
            package_path.write_bytes(b"compressed package data")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "dep-789",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "PENDING",
                }
            }
            mock_session.post.return_value = mock_response

            client = LifecycleCommandClient(mock_session)
            client.install_package(
                "test-workspace",
                package_path,
                variable_bindings=[
                    {
                        "uri": "https://example.com/crm#SyncBatchSize",
                        "value": "100",
                        "sourceMatrix": None,
                    }
                ],
                secret_bindings=[
                    {
                        "uri": "https://example.com/crm#ApiKey",
                        "keyId": "k-1",
                        "clientPublicKey": "ZmFrZQ==",
                        "encryptedValue": "ZmFrZQ==",
                        "nonce": "ZmFrZQ==",
                    }
                ],
            )

            files_arg = mock_session.post.call_args[1]["files"]
            assert "bindings" in files_arg
            payload = json.loads(files_arg["bindings"][1])
            assert payload["variableBindings"][0]["value"] == "100"
            assert payload["secretBindings"][0]["uri"] == (
                "https://example.com/crm#ApiKey"
            )

    def test_install_package_omits_bindings_when_empty(self):
        """Caller-friendliness: no bindings field is sent when none supplied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "package.tgz"
            package_path.write_bytes(b"compressed package data")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "dep-1",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "PENDING",
                }
            }
            mock_session.post.return_value = mock_response

            client = LifecycleCommandClient(mock_session)
            client.install_package("test-workspace", package_path)

            files_arg = mock_session.post.call_args[1]["files"]
            assert "bindings" not in files_arg


class TestLifecycleCommandClientInstallFromRegistry:
    """Test registry-based install (POST /registry/{publisher}/{name}/install)."""

    def _mock_response(self):
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "dep-reg-1",
                "packageName": "os",
                "version": "2.1.0",
                "status": "PENDING",
            }
        }
        return mock_response

    def test_install_from_registry_with_version_and_auto_update(self):
        mock_session = Mock()
        mock_session.post.return_value = self._mock_response()

        client = LifecycleCommandClient(mock_session)
        result = client.install_from_registry(
            "ws-1",
            "poliglot",
            "os",
            version="2.1.0",
            auto_update=True,
        )

        assert result.command_id == "dep-reg-1"
        assert result.package_name == "os"
        assert result.version == "2.1.0"
        assert result.status == "PENDING"

        call_args = mock_session.post.call_args
        assert call_args[0][0] == "/api/v1/packages/ws-1/registry/poliglot/os/install"
        assert call_args[1]["json"] == {"version": "2.1.0", "autoUpdate": True}

    def test_install_from_registry_omits_optional_fields_when_none(self):
        """Defaults stay server-side when version/auto_update aren't passed."""
        mock_session = Mock()
        mock_session.post.return_value = self._mock_response()

        client = LifecycleCommandClient(mock_session)
        client.install_from_registry("ws-1", "poliglot", "os")

        assert mock_session.post.call_args[1]["json"] == {}

    def test_install_from_registry_http_error(self):
        mock_session = Mock()
        mock_session.post.side_effect = requests.RequestException("boom")

        client = LifecycleCommandClient(mock_session)
        with pytest.raises(
            ServiceError, match="Failed to install package from registry"
        ):
            client.install_from_registry("ws-1", "poliglot", "os")


class TestLifecycleCommandClientListCommands:
    """Test listing commands functionality."""

    def test_list_commands_success(self):
        """Test successfully listing commands for a package."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "dep-1",
                    "packageInstallationId": "pkg-inst-1",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "COMPLETED",
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T00:00:00Z",
                },
                {
                    "id": "dep-2",
                    "packageInstallationId": "pkg-inst-1",
                    "packageName": "test-package",
                    "version": "1.0.1",
                    "status": "PENDING",
                    "createdAt": "2025-01-02T00:00:00Z",
                    "updatedAt": "2025-01-02T00:00:00Z",
                },
            ]
        }
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.list_commands("test-workspace", "test-package")

        assert len(result) == 2
        assert result[0].id == "dep-1"
        assert result[0].status == LifecycleCommandStatus.COMPLETED
        assert result[1].id == "dep-2"
        assert result[1].status == LifecycleCommandStatus.PENDING

    def test_list_commands_with_pagination(self):
        """Test listing commands with custom pagination."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        client.list_commands("test-workspace", "test-package", page=2, size=5)

        # Verify pagination params
        call_args = mock_session.get.call_args
        assert call_args[1]["params"]["page"] == 2
        assert call_args[1]["params"]["size"] == 5

    def test_list_commands_empty_result(self):
        """Test handling of empty command list."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.list_commands("test-workspace", "test-package")

        assert len(result) == 0

    def test_list_commands_http_error(self):
        """Test error handling when listing commands fails."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Network error")

        client = LifecycleCommandClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to list commands"):
            client.list_commands("test-workspace", "test-package")

    def test_list_commands_with_error_message(self):
        """Test parsing command with error message."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "dep-fail",
                    "packageInstallationId": "pkg-inst-1",
                    "packageName": "test-package",
                    "version": "1.0.0",
                    "status": "FAILED",
                    "errorMessage": "Validation failed",
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T00:00:00Z",
                }
            ]
        }
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.list_commands("test-workspace", "test-package")

        assert len(result) == 1
        assert result[0].status == LifecycleCommandStatus.FAILED
        assert result[0].error_message == "Validation failed"


class TestLifecycleCommandClientGetCommand:
    """Test single command retrieval."""

    def test_get_command_success(self):
        """Test successfully retrieving a single command."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "dep-123",
                "packageInstallationId": "pkg-inst-1",
                "packageName": "test-package",
                "version": "1.0.0",
                "status": "COMPLETED",
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-01T01:00:00Z",
            }
        }
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        command = client.get_command("test-workspace", "dep-123")

        assert command.id == "dep-123"
        assert command.package_name == "test-package"
        assert command.status == LifecycleCommandStatus.COMPLETED

    def test_get_command_http_error(self):
        """Test error when retrieving command fails."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Not found")

        client = LifecycleCommandClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch command"):
            client.get_command("test-workspace", "dep-nonexistent")


class TestLifecycleCommandClientEvents:
    """Test command event operations."""

    def test_get_command_events_success(self):
        """Test getting events for a command."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "evt-1",
                    "commandId": "dep-123",
                    "level": "INFO",
                    "message": "Command started",
                    "createdAt": "2025-01-01T00:00:00.000",
                },
                {
                    "id": "evt-2",
                    "commandId": "dep-123",
                    "level": "SUCCESS",
                    "message": "Validation completed",
                    "createdAt": "2025-01-01T00:01:00.000",
                },
            ]
        }
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.get_command_events("test-workspace", "dep-123")

        assert len(result) == 2
        assert result[0].level == LifecycleEventLevel.INFO
        assert result[1].level == LifecycleEventLevel.SUCCESS

    def test_get_command_events_empty(self):
        """Test getting events when none exist."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.get_command_events("test-workspace", "dep-123")

        assert len(result) == 0

    def test_get_command_events_http_error(self):
        """Test error when fetching events fails."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Network error")

        client = LifecycleCommandClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch events"):
            client.get_command_events("test-workspace", "dep-123")


class TestLifecycleCommandClientValidationReport:
    """Test validation report retrieval."""

    def test_get_validation_report_success(self):
        """Test successfully retrieving a validation report."""
        json_response = {
            "conforms": True,
            "violationCount": 0,
            "warningCount": 0,
            "infoCount": 0,
            "violations": [],
            "warnings": [],
            "infos": [],
        }
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = json_response
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.get_validation_report("test-workspace", "dep-123")

        assert result is not None
        assert result.conforms is True
        assert result.violation_count == 0
        mock_session.get.assert_called_once()
        # Verify Accept header was set for JSON format
        call_args = mock_session.get.call_args
        assert call_args[1]["headers"]["Accept"] == "application/json"

    def test_get_validation_report_not_found(self):
        """Test validation report returns None for 404."""
        mock_session = Mock()
        mock_session.get.side_effect = ResourceNotFoundError("Not found")

        client = LifecycleCommandClient(mock_session)
        result = client.get_validation_report("test-workspace", "dep-123")

        assert result is None

    def test_get_validation_report_http_error(self):
        """Test validation report propagates non-404 HTTP errors."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Server error")

        client = LifecycleCommandClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch validation report"):
            client.get_validation_report("test-workspace", "dep-123")

    def test_get_validation_report_with_violations(self):
        """Test retrieving validation report with violations."""
        json_response = {
            "conforms": False,
            "violationCount": 1,
            "warningCount": 0,
            "infoCount": 0,
            "violations": [
                {
                    "focusNode": "http://example.com/matrix/test",
                    "path": "http://example.com/prop",
                    "value": None,
                    "message": "Property required but missing",
                }
            ],
            "warnings": [],
            "infos": [],
        }
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = json_response
        mock_session.get.return_value = mock_response

        client = LifecycleCommandClient(mock_session)
        result = client.get_validation_report("test-workspace", "dep-123")

        assert result is not None
        assert result.conforms is False
        assert result.violation_count == 1
        assert len(result.violations) == 1
        assert result.violations[0].message == "Property required but missing"


class TestLifecycleCommandClientDateParsing:
    """Test datetime parsing functionality."""

    def test_parse_datetime_with_z_suffix(self):
        """Test parsing datetime with Z suffix."""
        mock_session = Mock()
        client = LifecycleCommandClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00Z")

        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 1
        assert dt.hour == 12
        assert dt.minute == 30
        assert dt.tzinfo is not None

    def test_parse_datetime_with_offset(self):
        """Test parsing datetime with timezone offset."""
        mock_session = Mock()
        client = LifecycleCommandClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00+00:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None

    def test_parse_datetime_naive(self):
        """Test parsing naive datetime assumes UTC."""
        mock_session = Mock()
        client = LifecycleCommandClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None  # Should be set to UTC
