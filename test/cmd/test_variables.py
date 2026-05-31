"""Unit tests for variables commands.

Tests cover the CLI commands for managing workspace variables: list, get,
and set (including clearing optional variables), URI and QName identifier
resolution, local identifier validation, and API error handling.
"""

from unittest.mock import Mock, patch

from plgt.cmd.variables import app
from plgt.core.exceptions import ResourceNotFoundError, ServiceError
from plgt.models.variable import Variable
from typer.testing import CliRunner

runner = CliRunner()

URI_FAST = "https://poliglot.io/os/spec#DefaultFastModel"
URI_OPTIONAL = "https://poliglot.io/os/spec#OptionalTuning"


def make_variable(
    identifier: str = "11111111-1111-1111-1111-111111111111",
    uri: str = URI_FAST,
    value: str | None = "openai:gpt-4o-mini",
    *,
    has_value: bool = True,
    label: str | None = "Default Fast Model",
    required: bool = True,
) -> Variable:
    """Create a test Variable object."""
    return Variable(
        id=identifier,
        uri=uri,
        value=value,
        has_value=has_value,
        variable_type="https://poliglot.io/os/spec#ModelRef",
        label=label,
        required=required,
    )


def _authed(mock_config, workspace: str | None = "test-workspace") -> Mock:
    """Wire a mock config to an authenticated session with a default workspace."""
    mock_session = Mock()
    mock_session.authenticated = True
    mock_config.get_session.return_value = mock_session
    mock_config.defaults.get.return_value = workspace
    return mock_session


class TestList:
    """Test variables list command."""

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_list_success_shows_table(self, mock_client_class, mock_config):
        """Test list displays variables in a table with value status."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [
            make_variable(),
            make_variable(
                identifier="22222222-2222-2222-2222-222222222222",
                uri=URI_OPTIONAL,
                value=None,
                has_value=False,
                label="Optional Tuning",
                required=False,
            ),
        ]

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        # The URI column carries the variable identity; the value column may be
        # wrapped by Rich at narrow widths, so assert identity + status here and
        # cover full value rendering in the `get` tests.
        assert "DefaultFastModel" in result.output
        assert "OptionalTuning" in result.output
        mock_client.list_variables.assert_called_once_with("test-workspace")

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_list_empty_shows_message(self, mock_client_class, mock_config):
        """Test list shows a message when there are no variables."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = []

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No variables found" in result.output

    @patch("plgt.cmd.variables.config")
    def test_list_no_workspace_no_default_exits(self, mock_config):
        """Test list exits when no workspace and no default configured."""
        _authed(mock_config, workspace=None)

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "No workspace specified" in result.output

    @patch("plgt.cmd.variables.config")
    def test_list_not_authenticated_exits(self, mock_config):
        """Test list exits when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_list_service_error(self, mock_client_class, mock_config):
        """Test list reports a ServiceError and exits non-zero."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.side_effect = ServiceError("Forbidden")

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
        assert "Failed to list variables" in result.output


class TestGet:
    """Test variables get command."""

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_get_by_qname(self, mock_client_class, mock_config):
        """Test get resolves a QName to a variable and shows its value."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]

        result = runner.invoke(app, ["get", "plgt:DefaultFastModel"])

        assert result.exit_code == 0
        assert URI_FAST in result.output
        assert "openai:gpt-4o-mini" in result.output
        assert "11111111-1111-1111-1111-111111111111" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_get_by_full_uri(self, mock_client_class, mock_config):
        """Test get resolves a full URI exactly."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]

        result = runner.invoke(app, ["get", URI_FAST])

        assert result.exit_code == 0
        assert "openai:gpt-4o-mini" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_get_not_found_exits(self, mock_client_class, mock_config):
        """Test get exits when the identifier matches no variable."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]

        result = runner.invoke(app, ["get", "plgt:DoesNotExist"])

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_get_malformed_identifier_rejected_locally(self):
        """Test get rejects a malformed identifier before any API call."""
        result = runner.invoke(app, ["get", "not a uri or qname"])

        assert result.exit_code == 1
        assert "not a valid variable identifier" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_get_ambiguous_local_name_exits(self, mock_client_class, mock_config):
        """Test get rejects a QName whose local name matches multiple variables."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [
            make_variable(uri="https://example.com/a#Shared"),
            make_variable(
                identifier="22222222-2222-2222-2222-222222222222",
                uri="https://example.com/b#Shared",
            ),
        ]

        result = runner.invoke(app, ["get", "x:Shared"])

        assert result.exit_code == 1
        assert "ambiguous" in result.output


class TestSet:
    """Test variables set command."""

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_value_by_qname(self, mock_client_class, mock_config):
        """Test set resolves a QName to the variable id and PUTs the value."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]

        result = runner.invoke(app, ["set", "plgt:DefaultFastModel", "openai:gpt-4o"])

        assert result.exit_code == 0
        assert "Variable value updated" in result.output
        mock_client.set_variable_value.assert_called_once_with(
            "test-workspace",
            "11111111-1111-1111-1111-111111111111",
            "openai:gpt-4o",
        )

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_clear_optional_sends_null(self, mock_client_class, mock_config):
        """Test --clear on an optional variable sends a null value."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [
            make_variable(
                identifier="22222222-2222-2222-2222-222222222222",
                uri=URI_OPTIONAL,
                value="something",
                required=False,
            )
        ]

        result = runner.invoke(app, ["set", "plgt:OptionalTuning", "--clear"])

        assert result.exit_code == 0
        assert "Variable value cleared" in result.output
        mock_client.set_variable_value.assert_called_once_with(
            "test-workspace",
            "22222222-2222-2222-2222-222222222222",
            None,
        )

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_clear_required_rejected(self, mock_client_class, mock_config):
        """Test --clear on a required variable is rejected without an API call."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable(required=True)]

        result = runner.invoke(app, ["set", "plgt:DefaultFastModel", "--clear"])

        assert result.exit_code == 1
        assert "required and cannot be cleared" in result.output
        mock_client.set_variable_value.assert_not_called()

    def test_set_no_value_no_clear_rejected(self):
        """Test set with neither a value nor --clear is rejected locally."""
        result = runner.invoke(app, ["set", "plgt:DefaultFastModel"])

        assert result.exit_code == 1
        assert "No value provided" in result.output

    def test_set_value_and_clear_rejected(self):
        """Test set rejects passing both a value and --clear."""
        result = runner.invoke(app, ["set", "plgt:DefaultFastModel", "x", "--clear"])

        assert result.exit_code == 1
        assert "Cannot pass both" in result.output

    def test_set_malformed_identifier_rejected_locally(self):
        """Test set rejects a malformed identifier before any API call."""
        result = runner.invoke(app, ["set", "bad identifier", "value"])

        assert result.exit_code == 1
        assert "not a valid variable identifier" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_not_found_exits(self, mock_client_class, mock_config):
        """Test set exits when the identifier matches no variable."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]

        result = runner.invoke(app, ["set", "plgt:Missing", "value"])

        assert result.exit_code == 1
        assert "not found" in result.output
        mock_client.set_variable_value.assert_not_called()

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_value_endpoint_404_exits(self, mock_client_class, mock_config):
        """Test set surfaces a 404 from the value endpoint as an error."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]
        mock_client.set_variable_value.side_effect = ResourceNotFoundError("Not found.")

        result = runner.invoke(app, ["set", "plgt:DefaultFastModel", "openai:gpt-4o"])

        assert result.exit_code == 1
        assert "Failed to set variable" in result.output

    @patch("plgt.cmd.variables.config")
    @patch("plgt.cmd.variables.VariablesClient")
    def test_set_service_error(self, mock_client_class, mock_config):
        """Test set reports a ServiceError from the value endpoint."""
        _authed(mock_config)
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_variables.return_value = [make_variable()]
        mock_client.set_variable_value.side_effect = ServiceError("boom")

        result = runner.invoke(app, ["set", "plgt:DefaultFastModel", "openai:gpt-4o"])

        assert result.exit_code == 1
        assert "Failed to set variable" in result.output


class TestArgumentNames:
    """Verify the identifier argument is surfaced as a URI, not an ID."""

    def test_get_help_uses_variable_uri(self):
        """Test get exposes the identifier as VARIABLE_URI in --help."""
        result = runner.invoke(app, ["get", "--help"])

        assert result.exit_code == 0
        assert "VARIABLE_URI" in result.output
        assert "URI or QName" in result.output

    def test_set_help_uses_variable_uri(self):
        """Test set exposes the identifier as VARIABLE_URI in --help."""
        result = runner.invoke(app, ["set", "--help"])

        assert result.exit_code == 0
        assert "VARIABLE_URI" in result.output
        assert "URI or QName" in result.output


class TestIdentifierValidation:
    """Direct coverage of the local identifier validation helper."""

    def test_full_uri_accepted(self):
        from plgt.cmd.variables import _validate_identifier

        # Should not raise.
        _validate_identifier("https://example.com/ns#Foo")

    def test_qname_accepted(self):
        from plgt.cmd.variables import _validate_identifier

        _validate_identifier("plgt:DefaultFastModel")

    def test_bare_word_rejected(self):
        from plgt.cmd.variables import _validate_identifier
        from plgt.core.exceptions import ValidationError

        import pytest

        with pytest.raises(ValidationError):
            _validate_identifier("JustAWord")

    def test_whitespace_rejected(self):
        from plgt.cmd.variables import _validate_identifier
        from plgt.core.exceptions import ValidationError

        import pytest

        with pytest.raises(ValidationError):
            _validate_identifier("plgt: Default")
