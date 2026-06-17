"""Unit tests for extension_client module.

Tests cover ExtensionClient functionality including creating, listing, getting,
updating, and deleting matrix extensions.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from plgt.clients.extension_client import Extension, ExtensionClient
from plgt.core.exceptions import ServiceError


class TestCreateExtension:
    """Test extension creation functionality."""

    def test_create_with_multipart(self):
        """Test creating extension sends multipart request with file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "extension.ttl"
            file_path.write_text(
                "@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\ntest:Role a plgt-iam:Role ."
            )

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "ext-123",
                    "label": "Test Extension",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": False,
                    "owner": {"id": "user-123", "username": "testuser"},
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T00:00:00Z",
                }
            }
            mock_session.post.return_value = mock_response

            client = ExtensionClient(mock_session)
            result = client.create_extension(
                "test-workspace",
                "https://test.example/matrix#",
                file_path,
            )

            assert result.id == "ext-123"
            assert result.label == "Test Extension"
            assert result.active is False
            mock_session.post.assert_called_once()
            call_args = mock_session.post.call_args
            assert "/api/v1/extensions/test-workspace" in call_args[0][0]

    def test_create_includes_label_when_provided(self):
        """Test create includes label in form data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "extension.ttl"
            file_path.write_text("@prefix test: <http://test.com> .")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "ext-123",
                    "label": "Custom Label",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": False,
                    "owner": {"id": "user-123", "username": "testuser"},
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T00:00:00Z",
                }
            }
            mock_session.post.return_value = mock_response

            client = ExtensionClient(mock_session)
            result = client.create_extension(
                "test-workspace",
                "https://test.example/matrix#",
                file_path,
                label="Custom Label",
            )

            assert result.label == "Custom Label"
            # Verify label was passed in data
            call_args = mock_session.post.call_args
            assert call_args[1]["data"]["label"] == "Custom Label"

    def test_create_parses_response(self):
        """Test create parses Extension from response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "extension.ttl"
            file_path.write_text("@prefix test: <http://test.com> .")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "ext-456",
                    "label": "My Extension",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": True,
                    "owner": {"id": "user-789", "username": "admin"},
                    "content": "@prefix test: <http://test.com> .",
                    "createdAt": "2025-01-01T12:30:00Z",
                    "updatedAt": "2025-01-02T14:00:00Z",
                }
            }
            mock_session.post.return_value = mock_response

            client = ExtensionClient(mock_session)
            result = client.create_extension(
                "workspace",
                "https://test.example/matrix#",
                file_path,
            )

            assert isinstance(result, Extension)
            assert result.id == "ext-456"
            assert result.label == "My Extension"
            assert result.target_matrix_uri == "https://test.example/matrix#"
            assert result.active is True
            assert result.owner_id == "user-789"
            assert result.owner_username == "admin"

    def test_create_http_error(self):
        """Test create raises ServiceError on HTTP error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "extension.ttl"
            file_path.write_text("@prefix test: <http://test.com> .")

            mock_session = Mock()
            mock_session.post.side_effect = requests.RequestException("Network error")

            client = ExtensionClient(mock_session)

            with pytest.raises(ServiceError, match="Failed to create extension"):
                client.create_extension(
                    "workspace",
                    "https://test.example/matrix#",
                    file_path,
                )


class TestListExtensions:
    """Test extension listing functionality."""

    def test_list_returns_list(self):
        """Test list returns list of Extension objects."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "ext-1",
                    "label": "Extension 1",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": True,
                    "owner": {"id": "user-1", "username": "user1"},
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T00:00:00Z",
                },
                {
                    "id": "ext-2",
                    "label": "Extension 2",
                    "targetMatrix": {"uri": "https://test.example/other#"},
                    "active": False,
                    "owner": {"id": "user-2", "username": "user2"},
                    "createdAt": "2025-01-02T00:00:00Z",
                    "updatedAt": "2025-01-02T00:00:00Z",
                },
            ]
        }
        mock_session.get.return_value = mock_response

        client = ExtensionClient(mock_session)
        result = client.list_extensions("test-workspace")

        assert len(result) == 2
        assert all(isinstance(e, Extension) for e in result)
        assert result[0].id == "ext-1"
        assert result[0].active is True
        assert result[1].id == "ext-2"
        assert result[1].active is False

    def test_list_handles_empty_response(self):
        """Test list returns empty list when no extensions."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = ExtensionClient(mock_session)
        result = client.list_extensions("test-workspace")

        assert len(result) == 0
        assert result == []

    def test_list_http_error(self):
        """Test list raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Network error")

        client = ExtensionClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to list extensions"):
            client.list_extensions("test-workspace")


class TestGetExtension:
    """Test single extension retrieval."""

    def test_get_returns_with_content(self):
        """Test get returns Extension with content field."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "ext-123",
                "label": "My Extension",
                "targetMatrix": {"uri": "https://test.example/matrix#"},
                "active": True,
                "owner": {"id": "user-1", "username": "admin"},
                "content": "@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\ntest:Role a plgt-iam:Role .",
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-01T00:00:00Z",
            }
        }
        mock_session.get.return_value = mock_response

        client = ExtensionClient(mock_session)
        result = client.get_extension("test-workspace", "ext-123")

        assert result.id == "ext-123"
        assert (
            result.content
            == "@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\ntest:Role a plgt-iam:Role ."
        )
        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        assert "/api/v1/extensions/test-workspace/ext-123" in call_args[0][0]

    def test_get_http_error(self):
        """Test get raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Not found")

        client = ExtensionClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch extension"):
            client.get_extension("test-workspace", "nonexistent-id")


class TestUpdateExtension:
    """Test extension update functionality."""

    def test_update_with_file(self):
        """Test update sends file in multipart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "updated.ttl"
            file_path.write_text("@prefix updated: <http://updated.com> .")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "ext-123",
                    "label": "My Extension",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": False,
                    "owner": {"id": "user-1", "username": "admin"},
                    "content": "@prefix updated: <http://updated.com> .",
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-02T00:00:00Z",
                }
            }
            mock_session.put.return_value = mock_response

            client = ExtensionClient(mock_session)
            result = client.update_extension("test-workspace", "ext-123", file_path)

            assert result.active is False  # Should be pending after update
            mock_session.put.assert_called_once()
            call_args = mock_session.put.call_args
            assert call_args[1]["files"] is not None

    def test_update_with_label_only(self):
        """Test update sends label without file."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "ext-123",
                "label": "New Label",
                "targetMatrix": {"uri": "https://test.example/matrix#"},
                "active": False,
                "owner": {"id": "user-1", "username": "admin"},
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-02T00:00:00Z",
            }
        }
        mock_session.put.return_value = mock_response

        client = ExtensionClient(mock_session)
        result = client.update_extension("test-workspace", "ext-123", label="New Label")

        assert result.label == "New Label"
        call_args = mock_session.put.call_args
        # Label is sent as multipart form field in files dict
        assert call_args[1]["files"]["label"] == (None, "New Label")
        assert "file" not in call_args[1]["files"]

    def test_update_with_both(self):
        """Test update sends both file and label."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "updated.ttl"
            file_path.write_text("@prefix updated: <http://updated.com> .")

            mock_session = Mock()
            mock_response = Mock()
            mock_response.json.return_value = {
                "data": {
                    "id": "ext-123",
                    "label": "Updated Label",
                    "targetMatrix": {"uri": "https://test.example/matrix#"},
                    "active": False,
                    "owner": {"id": "user-1", "username": "admin"},
                    "content": "@prefix updated: <http://updated.com> .",
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-02T00:00:00Z",
                }
            }
            mock_session.put.return_value = mock_response

            client = ExtensionClient(mock_session)
            result = client.update_extension(
                "test-workspace", "ext-123", file_path, label="Updated Label"
            )

            assert result.label == "Updated Label"
            call_args = mock_session.put.call_args
            # Both file and label are sent in files dict as multipart form data
            assert "file" in call_args[1]["files"]
            assert call_args[1]["files"]["label"] == (None, "Updated Label")

    def test_update_http_error(self):
        """Test update raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.put.side_effect = requests.RequestException("Server error")

        client = ExtensionClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to update extension"):
            client.update_extension("test-workspace", "ext-123", label="New Label")


class TestDeleteExtension:
    """Test extension deletion functionality."""

    def test_delete_sends_request(self):
        """Test delete sends DELETE request to correct URL."""
        mock_session = Mock()
        mock_response = Mock()
        mock_session.delete.return_value = mock_response

        client = ExtensionClient(mock_session)
        client.delete_extension("test-workspace", "ext-123")

        mock_session.delete.assert_called_once()
        call_args = mock_session.delete.call_args
        assert "/api/v1/extensions/test-workspace/ext-123" in call_args[0][0]

    def test_delete_http_error(self):
        """Test delete raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.delete.side_effect = requests.RequestException("Not found")

        client = ExtensionClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to delete extension"):
            client.delete_extension("test-workspace", "nonexistent-id")


class TestDatetimeParsing:
    """Test datetime parsing functionality."""

    def test_parse_datetime_with_z_suffix(self):
        """Test parsing datetime with Z suffix."""
        mock_session = Mock()
        client = ExtensionClient(mock_session)

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
        client = ExtensionClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00+00:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None

    def test_parse_datetime_naive(self):
        """Test parsing naive datetime assumes UTC."""
        mock_session = Mock()
        client = ExtensionClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None  # Should be set to UTC
