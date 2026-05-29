"""Unit tests for extension commands.

Tests cover CLI commands for managing matrix extensions including create, list, get,
update, and delete operations.
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from plgt.clients.extension_client import Extension
from plgt.cmd.extensions import app
from plgt.core.exceptions import ServiceError
from typer.testing import CliRunner

runner = CliRunner()


def create_test_extension(
    ext_id: str = "750e8400-e29b-41d4-a716-446655440000",
    label: str = "Test Extension",
    target_matrix_uri: str = "https://test.example/matrix#",
    *,
    active: bool = False,
    content: str | None = None,
) -> Extension:
    """Create a test Extension object."""
    return Extension(
        id=ext_id,
        label=label,
        target_matrix_uri=target_matrix_uri,
        active=active,
        owner_id="550e8400-e29b-41d4-a716-446655440000",
        owner_username="testuser",
        content=content,
        created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


class TestCreate:
    """Test extension create command."""

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_create_success(self, mock_client_class, mock_config):
        """Test successful extension creation."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.create_extension.return_value = create_test_extension()

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "create",
                "--matrix",
                "https://test.example/matrix#",
                "--file",
                temp_path,
                "--label",
                "Test Extension",
            ],
        )

        assert result.exit_code == 0
        assert "Extension created successfully" in result.output
        assert "750e8400-e29b-41d4-a716-446655440000" in result.output
        mock_client.create_extension.assert_called_once()

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_create_with_explicit_workspace(self, mock_client_class, mock_config):
        """Test create with explicit workspace option."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.create_extension.return_value = create_test_extension()

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "create",
                "--workspace",
                "explicit-workspace",
                "--matrix",
                "https://test.example/matrix#",
                "--file",
                temp_path,
            ],
        )

        assert result.exit_code == 0
        # Verify workspace was passed to client
        call_args = mock_client.create_extension.call_args
        assert call_args[0][0] == "explicit-workspace"

    @patch("plgt.cmd.extensions.config")
    def test_create_no_workspace_no_default_exits(self, mock_config):
        """Test create exits when no workspace and no default."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = None  # No default workspace

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "create",
                "--matrix",
                "https://test.example/matrix#",
                "--file",
                temp_path,
            ],
        )

        assert result.exit_code == 1
        assert "No workspace specified" in result.output

    @patch("plgt.cmd.extensions.config")
    def test_create_not_authenticated_exits(self, mock_config):
        """Test create exits when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "create",
                "--matrix",
                "https://test.example/matrix#",
                "--file",
                temp_path,
            ],
        )

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_create_service_error_displays_error(self, mock_client_class, mock_config):
        """Test create displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.create_extension.side_effect = ServiceError("Matrix not found")

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "create",
                "--matrix",
                "https://test.example/matrix#",
                "--file",
                temp_path,
            ],
        )

        assert result.exit_code == 1
        assert "Failed to create extension" in result.output


class TestList:
    """Test extension list command."""

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_list_success_shows_table(self, mock_client_class, mock_config):
        """Test list displays extensions in table format."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_extensions.return_value = [
            create_test_extension(active=True),
            create_test_extension(
                ext_id="850e8400-e29b-41d4-a716-446655440000",
                label="Another Extension",
                active=False,
            ),
        ]

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        # Check for extension IDs (not truncated in table)
        assert "750e8400-e29b-41d4-a716-446655440000" in result.output
        assert "850e8400-e29b-41d4-a716-446655440000" in result.output
        # Check for status indicators (may be truncated to "acti" and "pend")
        assert "acti" in result.output
        assert "pend" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_list_empty_shows_message(self, mock_client_class, mock_config):
        """Test list shows message when no extensions found."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_extensions.return_value = []

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No extensions found" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_list_service_error(self, mock_client_class, mock_config):
        """Test list displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_extensions.side_effect = ServiceError("Forbidden")

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "Failed to list extensions" in result.output


class TestGet:
    """Test extension get command."""

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_get_displays_details(self, mock_client_class, mock_config):
        """Test get displays extension details and content."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_extension.return_value = create_test_extension(
            content="@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\ntest:Role a plgt-iam:Role ."
        )

        result = runner.invoke(app, ["get", "750e8400-e29b-41d4-a716-446655440000"])

        assert result.exit_code == 0
        assert "Test Extension" in result.output
        assert "https://test.example/matrix#" in result.output
        assert "Content:" in result.output
        assert "test:Role" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_get_with_output_writes_file(self, mock_client_class, mock_config):
        """Test get writes content to file with --output."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        test_content = "@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\ntest:Role a plgt-iam:Role ."
        mock_client.get_extension.return_value = create_test_extension(
            content=test_content
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "output.ttl"

            result = runner.invoke(
                app,
                [
                    "get",
                    "750e8400-e29b-41d4-a716-446655440000",
                    "--output",
                    str(output_path),
                ],
            )

            assert result.exit_code == 0
            assert "Content written to" in result.output
            assert output_path.read_text() == test_content

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_get_not_found_exits(self, mock_client_class, mock_config):
        """Test get exits with error on 404."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_extension.side_effect = ServiceError("Extension not found")

        result = runner.invoke(app, ["get", "nonexistent-id"])

        assert result.exit_code == 1
        assert "Failed to get extension" in result.output


class TestUpdate:
    """Test extension update command."""

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_update_file_only_success(self, mock_client_class, mock_config):
        """Test update with file only."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.update_extension.return_value = create_test_extension()

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            ["update", "750e8400-e29b-41d4-a716-446655440000", "--file", temp_path],
        )

        assert result.exit_code == 0
        assert "Extension updated successfully" in result.output
        # Verify file was passed
        call_args = mock_client.update_extension.call_args
        assert call_args[0][2] is not None  # file_path

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_update_label_only_success(self, mock_client_class, mock_config):
        """Test update with label only."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        updated_ext = create_test_extension(label="Updated Label")
        mock_client.update_extension.return_value = updated_ext

        result = runner.invoke(
            app,
            [
                "update",
                "750e8400-e29b-41d4-a716-446655440000",
                "--label",
                "Updated Label",
            ],
        )

        assert result.exit_code == 0
        assert "Extension updated successfully" in result.output
        assert "Updated Label" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_update_both_success(self, mock_client_class, mock_config):
        """Test update with both file and label."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.update_extension.return_value = create_test_extension(
            label="New Label"
        )

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as f:
            f.write(
                b"@prefix plgt-iam: <https://poliglot.io/os/spec/iam#> .\n@prefix test: <http://test.com> .\n"
            )
            temp_path = f.name

        result = runner.invoke(
            app,
            [
                "update",
                "750e8400-e29b-41d4-a716-446655440000",
                "--file",
                temp_path,
                "--label",
                "New Label",
            ],
        )

        assert result.exit_code == 0
        assert "Extension updated successfully" in result.output

    @patch("plgt.cmd.extensions.config")
    def test_update_neither_exits(self, mock_config):
        """Test update exits when neither --file nor --label provided."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(
            app,
            ["update", "750e8400-e29b-41d4-a716-446655440000"],
        )

        assert result.exit_code == 1
        assert "At least one of --file or --label must be provided" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_update_shows_pending_status(self, mock_client_class, mock_config):
        """Test update shows pending status after update."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.update_extension.return_value = create_test_extension(active=False)

        result = runner.invoke(
            app,
            ["update", "750e8400-e29b-41d4-a716-446655440000", "--label", "Updated"],
        )

        assert result.exit_code == 0
        assert "pending" in result.output


class TestDelete:
    """Test extension delete command."""

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_delete_with_yes_flag_skips_prompt(self, mock_client_class, mock_config):
        """Test delete with --yes skips confirmation."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client

        result = runner.invoke(
            app,
            ["delete", "750e8400-e29b-41d4-a716-446655440000", "--yes"],
        )

        assert result.exit_code == 0
        assert "Extension deleted successfully" in result.output
        mock_client.delete_extension.assert_called_once()

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_delete_with_confirmation(self, mock_client_class, mock_config):
        """Test delete prompts for confirmation."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client

        # Simulate user typing 'y' at prompt
        result = runner.invoke(
            app,
            ["delete", "750e8400-e29b-41d4-a716-446655440000"],
            input="y\n",
        )

        assert result.exit_code == 0
        assert "Extension deleted successfully" in result.output

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_delete_cancelled_exits(self, mock_client_class, mock_config):
        """Test delete exits when user declines confirmation."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client

        # Simulate user typing 'n' at prompt
        result = runner.invoke(
            app,
            ["delete", "750e8400-e29b-41d4-a716-446655440000"],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_client.delete_extension.assert_not_called()

    @patch("plgt.cmd.extensions.config")
    @patch("plgt.cmd.extensions.ExtensionClient")
    def test_delete_service_error(self, mock_client_class, mock_config):
        """Test delete displays error on ServiceError."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.delete_extension.side_effect = ServiceError("Extension not found")

        result = runner.invoke(
            app,
            ["delete", "750e8400-e29b-41d4-a716-446655440000", "--yes"],
        )

        assert result.exit_code == 1
        assert "Failed to delete extension" in result.output
