"""Unit tests for secrets commands.

Tests cover CLI commands for managing secrets including list, get, and set operations.
"""

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from plgt.cmd.secrets import app
from plgt.core.exceptions import ResourceNotFoundError, ServiceError
from plgt.models.secret import Secret
from typer.testing import CliRunner

runner = CliRunner()


def create_test_secret(
    identifier: str = "mymatrix:OpenAIAPIKey",
    uri: str = "https://example.com/mymatrix#OpenAIAPIKey",
    description: str = "API key for OpenAI integration",
    *,
    has_value: bool = True,
    access_count: int = 5,
) -> Secret:
    """Create a test Secret object."""
    return Secret(
        id=identifier,
        uri=uri,
        description=description,
        has_value=has_value,
        created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, 14, 0, 0, tzinfo=UTC),
        last_accessed_at=datetime(2025, 1, 1, 13, 0, 0, tzinfo=UTC)
        if has_value
        else None,
        access_count=access_count,
    )


class TestList:
    """Test secrets list command."""

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_list_success_shows_table(self, mock_client_class, mock_config):
        """Test list displays secrets in table format."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_secrets.return_value = [
            create_test_secret(has_value=True),
            create_test_secret(
                identifier="mymatrix:DatabasePassword",
                uri="https://example.com/mymatrix#DatabasePassword",
                description="Database password",
                has_value=False,
            ),
        ]

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "mymatrix:OpenAIAPIKey" in result.output
        assert "mymatrix:DatabasePassword" in result.output
        # Check for status indicators
        assert "Yes" in result.output
        assert "No" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_list_with_prefix(self, mock_client_class, mock_config):
        """Test list passes prefix to client."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_secrets.return_value = [create_test_secret()]

        result = runner.invoke(app, ["list", "--prefix", "mymatrix:"])

        assert result.exit_code == 0
        mock_client.list_secrets.assert_called_once_with(
            "test-workspace", prefix="mymatrix:"
        )

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_list_empty_shows_message(self, mock_client_class, mock_config):
        """Test list shows message when no secrets found."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_secrets.return_value = []

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No secrets found" in result.output

    @patch("plgt.cmd.secrets.config")
    def test_list_no_workspace_no_default_exits(self, mock_config):
        """Test list exits when no workspace and no default."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = None

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "No workspace specified" in result.output

    @patch("plgt.cmd.secrets.config")
    def test_list_not_authenticated_exits(self, mock_config):
        """Test list exits when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_list_service_error(self, mock_client_class, mock_config):
        """Test list displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_secrets.side_effect = ServiceError("Forbidden")

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "Failed to list secrets" in result.output


class TestGet:
    """Test secrets get command."""

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_get_displays_metadata(self, mock_client_class, mock_config):
        """Test get displays secret metadata."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.return_value = create_test_secret()

        result = runner.invoke(app, ["get", "mymatrix:OpenAIAPIKey"])

        assert result.exit_code == 0
        assert "mymatrix:OpenAIAPIKey" in result.output
        assert "API key for OpenAI integration" in result.output
        assert "Has Value:" in result.output
        assert "Access Count:" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_get_with_value_flag(self, mock_client_class, mock_config):
        """Test get with --value retrieves decrypted value."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret_value.return_value = "sk-test-api-key-12345"

        result = runner.invoke(app, ["get", "mymatrix:OpenAIAPIKey", "--value"])

        assert result.exit_code == 0
        assert "sk-test-api-key-12345" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_get_not_found_exits(self, mock_client_class, mock_config):
        """Test get exits with error on 404."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.side_effect = ResourceNotFoundError("Not found")

        result = runner.invoke(app, ["get", "nonexistent:Secret"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_get_value_not_found_exits(self, mock_client_class, mock_config):
        """Test get --value exits with error on 404."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret_value.side_effect = ResourceNotFoundError("Not found")

        result = runner.invoke(app, ["get", "nonexistent:Secret", "--value"])

        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_get_service_error(self, mock_client_class, mock_config):
        """Test get displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.side_effect = ServiceError("Server error")

        result = runner.invoke(app, ["get", "mymatrix:OpenAIAPIKey"])

        assert result.exit_code == 1
        assert "Failed to get secret" in result.output


class TestSet:
    """Test secrets set command."""

    @patch("plgt.cmd.secrets.sys")
    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_set_interactive_success(self, mock_client_class, mock_config, mock_sys):
        """Test set with interactive prompt."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.return_value = create_test_secret()

        # Mock sys.stdin.isatty() to return True for interactive mode
        mock_sys.stdin.isatty.return_value = True

        result = runner.invoke(
            app,
            ["set", "mymatrix:OpenAIAPIKey"],
            input="my-secret-value\n",
        )

        assert result.exit_code == 0
        assert "Secret value updated" in result.output
        mock_client.set_secret_value.assert_called_once_with(
            "test-workspace", "mymatrix:OpenAIAPIKey", "my-secret-value"
        )

    @patch("plgt.cmd.secrets.sys")
    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_set_from_stdin_success(self, mock_client_class, mock_config, mock_sys):
        """Test set with --from-stdin."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.return_value = create_test_secret()

        mock_sys.stdin.isatty.return_value = False
        mock_sys.stdin.read.return_value = "piped-secret-value\n"

        result = runner.invoke(
            app,
            ["set", "mymatrix:OpenAIAPIKey", "--from-stdin"],
        )

        assert result.exit_code == 0
        assert "Secret value updated" in result.output
        mock_client.set_secret_value.assert_called_once_with(
            "test-workspace", "mymatrix:OpenAIAPIKey", "piped-secret-value"
        )

    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_set_secret_not_found_exits(self, mock_client_class, mock_config):
        """Test set exits when secret doesn't exist."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.side_effect = ResourceNotFoundError("Not found")

        result = runner.invoke(
            app,
            ["set", "nonexistent:Secret"],
            input="value\n",
        )

        assert result.exit_code == 1
        assert "not found" in result.output
        mock_client.set_secret_value.assert_not_called()

    @patch("plgt.cmd.secrets.sys")
    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_set_from_stdin_empty_exits(self, mock_client_class, mock_config, mock_sys):
        """Test set --from-stdin exits when stdin is empty."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.return_value = create_test_secret()

        mock_sys.stdin.isatty.return_value = False
        mock_sys.stdin.read.return_value = ""

        result = runner.invoke(
            app,
            ["set", "mymatrix:OpenAIAPIKey", "--from-stdin"],
        )

        assert result.exit_code == 1
        assert "No value provided" in result.output

    @patch("plgt.cmd.secrets.sys")
    @patch("plgt.cmd.secrets.config")
    @patch("plgt.cmd.secrets.SecretsClient")
    def test_set_service_error(self, mock_client_class, mock_config, mock_sys):
        """Test set displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_secret.return_value = create_test_secret()
        mock_client.set_secret_value.side_effect = ServiceError("Server error")

        # Mock sys.stdin.isatty() to return True for interactive mode
        mock_sys.stdin.isatty.return_value = True

        result = runner.invoke(
            app,
            ["set", "mymatrix:OpenAIAPIKey"],
            input="value\n",
        )

        assert result.exit_code == 1
        assert "Failed to set secret" in result.output


class TestArgumentNames:
    """Verify the identifier argument is surfaced as a URI, not an ID."""

    def test_get_help_uses_secret_uri(self):
        """Test get exposes the identifier as SECRET_URI in --help."""
        result = runner.invoke(app, ["get", "--help"])

        assert result.exit_code == 0
        assert "SECRET_URI" in result.output
        assert "URI or QName" in result.output

    def test_set_help_uses_secret_uri(self):
        """Test set exposes the identifier as SECRET_URI in --help."""
        result = runner.invoke(app, ["set", "--help"])

        assert result.exit_code == 0
        assert "SECRET_URI" in result.output
        assert "URI or QName" in result.output


class TestExtractMatrixName:
    """Test URI parsing for matrix name extraction."""

    def test_extract_matrix_from_uri(self):
        """Test extracting matrix name from full URI."""
        from plgt.cmd.secrets import _extract_matrix_name

        result = _extract_matrix_name(
            "https://example.com/matrices/mymatrix#SecretName"
        )
        assert result == "mymatrix"

    def test_extract_matrix_from_uri_no_fragment(self):
        """Test extracting matrix name from URI without fragment."""
        from plgt.cmd.secrets import _extract_matrix_name

        result = _extract_matrix_name("https://example.com/matrices/mymatrix")
        assert result == "mymatrix"

    def test_extract_matrix_from_uri_trailing_slash(self):
        """Test extracting matrix name from URI with trailing slash."""
        from plgt.cmd.secrets import _extract_matrix_name

        result = _extract_matrix_name("https://example.com/matrices/mymatrix/")
        assert result == "mymatrix"
