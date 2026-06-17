"""Unit tests for secrets_client module.

Tests cover SecretsClient functionality including listing, getting,
and setting secret values with E2E encryption.
"""

from unittest.mock import Mock, patch

import pytest
import requests
from plgt.clients.secrets_client import SecretsClient
from plgt.core.exceptions import ServiceError
from plgt.models.secret import Secret


class TestListSecrets:
    """Test secrets listing functionality."""

    def test_list_returns_list(self):
        """Test list returns list of Secret objects."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "mymatrix:OpenAIAPIKey",
                    "uri": "https://example.com/mymatrix#OpenAIAPIKey",
                    "description": "API key for OpenAI integration",
                    "allowedScopes": ["workspace", "principal"],
                    "matrix": {
                        "uri": "https://example.com/mymatrix#",
                        "name": "mymatrix",
                    },
                    "createdAt": "2025-01-01T00:00:00Z",
                    "updatedAt": "2025-01-01T12:00:00Z",
                },
                {
                    "id": "mymatrix:DatabasePassword",
                    "uri": "https://example.com/mymatrix#DatabasePassword",
                    "description": "Database connection password",
                    "allowedScopes": ["workspace"],
                    "matrix": {
                        "uri": "https://example.com/mymatrix#",
                        "name": "mymatrix",
                    },
                    "createdAt": "2025-01-02T00:00:00Z",
                    "updatedAt": "2025-01-02T00:00:00Z",
                },
            ]
        }
        mock_session.get.return_value = mock_response

        client = SecretsClient(mock_session)
        result = client.list_secrets("test-workspace")

        assert len(result) == 2
        assert all(isinstance(s, Secret) for s in result)
        assert result[0].id == "mymatrix:OpenAIAPIKey"
        assert result[0].allowed_scopes == ["workspace", "principal"]
        assert result[0].matrix_name == "mymatrix"
        assert result[1].id == "mymatrix:DatabasePassword"
        assert result[1].allowed_scopes == ["workspace"]

    def test_list_with_prefix_passes_param(self):
        """Test list passes prefix parameter to API."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = SecretsClient(mock_session)
        client.list_secrets("test-workspace", prefix="mymatrix:")

        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        assert call_args[1]["params"]["prefix"] == "mymatrix:"

    def test_list_handles_empty_response(self):
        """Test list returns empty list when no secrets."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        client = SecretsClient(mock_session)
        result = client.list_secrets("test-workspace")

        assert len(result) == 0
        assert result == []

    def test_list_http_error(self):
        """Test list raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Network error")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to list secrets"):
            client.list_secrets("test-workspace")


class TestGetSecret:
    """Test single secret retrieval."""

    def test_get_returns_secret(self):
        """Test get returns Secret object."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "mymatrix:OpenAIAPIKey",
                "uri": "https://example.com/mymatrix#OpenAIAPIKey",
                "description": "API key for OpenAI integration",
                "allowedScopes": ["workspace", "principal"],
                "matrix": {
                    "uri": "https://example.com/mymatrix#",
                    "name": "mymatrix",
                },
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-01T12:00:00Z",
            }
        }
        mock_session.get.return_value = mock_response

        client = SecretsClient(mock_session)
        result = client.get_secret("test-workspace", "mymatrix:OpenAIAPIKey")

        assert result.id == "mymatrix:OpenAIAPIKey"
        assert result.description == "API key for OpenAI integration"
        assert result.allowed_scopes == ["workspace", "principal"]
        assert result.matrix_name == "mymatrix"
        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        assert "/api/v1/secrets/test-workspace/mymatrix:OpenAIAPIKey" in call_args[0][0]

    def test_get_http_error(self):
        """Test get raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Not found")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch secret"):
            client.get_secret("test-workspace", "nonexistent-id")


class TestGetSecretValue:
    """Test secret value retrieval with E2E decryption."""

    @patch("plgt.clients.secrets_client.generate_keypair")
    @patch("plgt.clients.secrets_client.public_key_to_base64")
    @patch("plgt.clients.secrets_client.parse_encrypted_response")
    @patch("plgt.clients.secrets_client.decrypt_secret_value")
    def test_get_value_decrypts_response(
        self,
        mock_decrypt,
        mock_parse,
        mock_to_base64,
        mock_generate,
    ):
        """Test get_secret_value performs E2E decryption."""
        mock_private_key = Mock()
        mock_public_key = Mock()
        mock_generate.return_value = (mock_private_key, mock_public_key)
        mock_to_base64.return_value = "base64-public-key"

        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "encryptedValue": "encrypted-data",
                "nonce": "nonce-data",
                "serverPublicKey": "server-key",
                "algorithm": "X25519_XCHACHA20_POLY1305",
            }
        }
        mock_session.get.return_value = mock_response

        mock_encrypted = Mock()
        mock_parse.return_value = mock_encrypted
        mock_decrypt.return_value = "decrypted-secret-value"

        client = SecretsClient(mock_session)
        result = client.get_secret_value("test-workspace", "mymatrix:OpenAIAPIKey")

        assert result == "decrypted-secret-value"

        # Verify ephemeral public key was sent in header
        call_args = mock_session.get.call_args
        assert call_args[1]["headers"]["X-Ephemeral-Pubkey"] == "base64-public-key"

        # Verify decryption was called
        mock_decrypt.assert_called_once_with(mock_encrypted, mock_private_key)

    @patch("plgt.clients.secrets_client.generate_keypair")
    @patch("plgt.clients.secrets_client.public_key_to_base64")
    @patch("plgt.clients.secrets_client.parse_encrypted_response")
    @patch("plgt.clients.secrets_client.decrypt_secret_value")
    def test_get_value_decryption_error(
        self,
        mock_decrypt,
        mock_parse,
        mock_to_base64,
        mock_generate,
    ):
        """Test get_secret_value raises ServiceError on decryption failure."""
        mock_private_key = Mock()
        mock_public_key = Mock()
        mock_generate.return_value = (mock_private_key, mock_public_key)
        mock_to_base64.return_value = "base64-public-key"

        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": {}}
        mock_session.get.return_value = mock_response

        mock_parse.side_effect = ValueError("Invalid response")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to decrypt secret value"):
            client.get_secret_value("test-workspace", "mymatrix:OpenAIAPIKey")

    def test_get_value_http_error(self):
        """Test get_secret_value raises ServiceError on HTTP error."""
        mock_session = Mock()
        mock_session.get.side_effect = requests.RequestException("Server error")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to fetch secret value"):
            client.get_secret_value("test-workspace", "mymatrix:OpenAIAPIKey")


class TestSetSecretValue:
    """Test secret value setting with E2E encryption."""

    def _mock_pubkey_session(self):
        """Create a mock session that returns a valid pubkey response."""
        import base64

        from nacl.public import PrivateKey

        server_private = PrivateKey.generate()
        server_public = server_private.public_key

        mock_session = Mock()
        mock_pubkey_response = Mock()
        mock_pubkey_response.json.return_value = {
            "data": {
                "serverPublicKey": base64.b64encode(bytes(server_public)).decode(
                    "ascii"
                ),
                "keyId": "test-key-id",
                "algorithm": "X25519-XChaCha20-Poly1305",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        }
        mock_session.post.return_value = mock_pubkey_response

        mock_put_response = Mock()
        mock_session.put.return_value = mock_put_response

        return mock_session

    def test_set_value_sends_encrypted_request(self):
        """Test set_secret_value posts to pubkey then PUTs encrypted payload."""
        mock_session = self._mock_pubkey_session()

        client = SecretsClient(mock_session)
        client.set_secret_value(
            "test-workspace", "mymatrix:OpenAIAPIKey", "secret-value"
        )

        # Verify pubkey POST
        mock_session.post.assert_called_once_with(
            "/api/v1/secrets/test-workspace/pubkey",
        )

        # Verify encrypted PUT
        mock_session.put.assert_called_once()
        call_args = mock_session.put.call_args
        assert (
            "/api/v1/secrets/test-workspace/mymatrix:OpenAIAPIKey/value"
            in call_args[0][0]
        )
        payload = call_args[1]["json"]
        assert "encryptedValue" in payload
        assert "nonce" in payload
        assert "clientPublicKey" in payload
        assert payload["keyId"] == "test-key-id"

    def test_set_value_pubkey_http_error(self):
        """Test set_secret_value raises ServiceError when pubkey POST fails."""
        mock_session = Mock()
        mock_session.post.side_effect = requests.RequestException("Server error")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to set secret value"):
            client.set_secret_value("test-workspace", "mymatrix:OpenAIAPIKey", "value")

    def test_set_value_put_http_error(self):
        """Test set_secret_value raises ServiceError when PUT fails."""
        mock_session = self._mock_pubkey_session()
        mock_session.put.side_effect = requests.RequestException("Server error")

        client = SecretsClient(mock_session)

        with pytest.raises(ServiceError, match="Failed to set secret value"):
            client.set_secret_value("test-workspace", "mymatrix:OpenAIAPIKey", "value")


class TestDatetimeParsing:
    """Test datetime parsing functionality."""

    def test_parse_datetime_with_z_suffix(self):
        """Test parsing datetime with Z suffix."""
        mock_session = Mock()
        client = SecretsClient(mock_session)

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
        client = SecretsClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00+00:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None

    def test_parse_datetime_naive(self):
        """Test parsing naive datetime assumes UTC."""
        mock_session = Mock()
        client = SecretsClient(mock_session)

        dt = client._parse_datetime("2025-01-01T12:30:00")

        assert dt.year == 2025
        assert dt.tzinfo is not None


class TestSecretParsing:
    """Test Secret object parsing."""

    def test_parse_secret_all_fields(self):
        """Test parsing secret with all fields."""
        mock_session = Mock()
        client = SecretsClient(mock_session)

        data = {
            "id": "mymatrix:OpenAIAPIKey",
            "uri": "https://example.com/mymatrix#OpenAIAPIKey",
            "description": "API key for OpenAI",
            "allowedScopes": ["workspace", "principal"],
            "matrix": {
                "uri": "https://example.com/mymatrix#",
                "name": "mymatrix",
            },
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T12:00:00Z",
        }

        secret = client._parse_secret(data)

        assert secret.id == "mymatrix:OpenAIAPIKey"
        assert secret.uri == "https://example.com/mymatrix#OpenAIAPIKey"
        assert secret.description == "API key for OpenAI"
        assert secret.allowed_scopes == ["workspace", "principal"]
        assert secret.matrix_uri == "https://example.com/mymatrix#"
        assert secret.matrix_name == "mymatrix"

    def test_parse_secret_missing_optional_fields(self):
        """Test parsing secret with missing optional fields uses defaults."""
        mock_session = Mock()
        client = SecretsClient(mock_session)

        data = {
            "id": "mymatrix:MinimalSecret",
            "uri": "https://example.com/mymatrix#MinimalSecret",
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T00:00:00Z",
        }

        secret = client._parse_secret(data)

        assert secret.description == ""
        assert secret.allowed_scopes == []
        assert secret.matrix_uri is None
        assert secret.matrix_name is None
