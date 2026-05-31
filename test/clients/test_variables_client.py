"""Unit tests for variables_client module.

Tests cover VariablesClient functionality including listing variables and
setting / clearing variable values.
"""

from unittest.mock import Mock

from plgt.clients.variables_client import VariablesClient
from plgt.models.variable import Variable


class TestListVariables:
    """Test variable listing functionality."""

    def test_list_returns_list(self):
        """Test list returns Variable objects parsed from a paged response."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "items": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "uri": "https://poliglot.io/os/spec#DefaultFastModel",
                        "value": "openai:gpt-4o-mini",
                        "hasValue": True,
                        "variableType": "https://poliglot.io/os/spec#ModelRef",
                        "label": "Default Fast Model",
                        "required": True,
                    },
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "uri": "https://poliglot.io/os/spec#OptionalTuning",
                        "value": None,
                        "hasValue": False,
                        "variableType": None,
                        "label": "Optional Tuning",
                        "required": False,
                    },
                ],
                "currentPage": 0,
                "totalPages": 1,
                "totalResults": 2,
            }
        }
        mock_session.get.return_value = mock_response

        client = VariablesClient(mock_session)
        result = client.list_variables("test-workspace")

        assert len(result) == 2
        assert all(isinstance(v, Variable) for v in result)
        assert result[0].id == "11111111-1111-1111-1111-111111111111"
        assert result[0].value == "openai:gpt-4o-mini"
        assert result[0].has_value is True
        assert result[0].required is True
        assert result[1].value is None
        assert result[1].has_value is False
        assert result[1].required is False

    def test_list_hits_expected_endpoint(self):
        """Test list targets the workspace-scoped variables endpoint."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": {"items": []}}
        mock_session.get.return_value = mock_response

        client = VariablesClient(mock_session)
        client.list_variables("test-workspace")

        mock_session.get.assert_called_once_with("/api/v1/variables/test-workspace")

    def test_list_handles_empty_response(self):
        """Test list returns an empty list when there are no variables."""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.json.return_value = {"data": {"items": []}}
        mock_session.get.return_value = mock_response

        client = VariablesClient(mock_session)
        result = client.list_variables("test-workspace")

        assert result == []


class TestSetVariableValue:
    """Test setting and clearing variable values."""

    def test_set_value_puts_to_value_endpoint(self):
        """Test set_variable_value PUTs the value keyed by variable id."""
        mock_session = Mock()
        mock_put_response = Mock()
        mock_put_response.json.return_value = {
            "data": {
                "id": "11111111-1111-1111-1111-111111111111",
                "uri": "https://poliglot.io/os/spec#DefaultFastModel",
                "value": "openai:gpt-4o-mini",
                "hasValue": True,
                "variableType": None,
                "label": "Default Fast Model",
                "required": True,
            }
        }
        mock_session.put.return_value = mock_put_response

        client = VariablesClient(mock_session)
        updated = client.set_variable_value(
            "test-workspace",
            "11111111-1111-1111-1111-111111111111",
            "openai:gpt-4o-mini",
        )

        mock_session.put.assert_called_once_with(
            "/api/v1/variables/test-workspace/11111111-1111-1111-1111-111111111111/value",
            json={"value": "openai:gpt-4o-mini"},
        )
        assert updated.value == "openai:gpt-4o-mini"
        assert updated.has_value is True

    def test_clear_value_sends_null(self):
        """Test clearing a variable sends ``{"value": null}``."""
        mock_session = Mock()
        mock_put_response = Mock()
        mock_put_response.json.return_value = {
            "data": {
                "id": "22222222-2222-2222-2222-222222222222",
                "uri": "https://poliglot.io/os/spec#OptionalTuning",
                "value": None,
                "hasValue": False,
                "variableType": None,
                "label": "Optional Tuning",
                "required": False,
            }
        }
        mock_session.put.return_value = mock_put_response

        client = VariablesClient(mock_session)
        updated = client.set_variable_value(
            "test-workspace",
            "22222222-2222-2222-2222-222222222222",
            None,
        )

        mock_session.put.assert_called_once_with(
            "/api/v1/variables/test-workspace/22222222-2222-2222-2222-222222222222/value",
            json={"value": None},
        )
        assert updated.value is None
        assert updated.has_value is False


class TestVariableParsing:
    """Test Variable object parsing."""

    def test_parse_variable_all_fields(self):
        """Test parsing a variable with all fields present."""
        mock_session = Mock()
        client = VariablesClient(mock_session)

        data = {
            "id": "11111111-1111-1111-1111-111111111111",
            "uri": "https://poliglot.io/os/spec#DefaultFastModel",
            "value": "openai:gpt-4o-mini",
            "hasValue": True,
            "variableType": "https://poliglot.io/os/spec#ModelRef",
            "label": "Default Fast Model",
            "required": True,
        }

        variable = client._parse_variable(data)

        assert variable.id == "11111111-1111-1111-1111-111111111111"
        assert variable.uri == "https://poliglot.io/os/spec#DefaultFastModel"
        assert variable.value == "openai:gpt-4o-mini"
        assert variable.has_value is True
        assert variable.variable_type == "https://poliglot.io/os/spec#ModelRef"
        assert variable.label == "Default Fast Model"
        assert variable.required is True

    def test_parse_variable_infers_has_value_from_value(self):
        """Test hasValue defaults to ``value is not None`` when absent."""
        mock_session = Mock()
        client = VariablesClient(mock_session)

        data = {
            "id": "33333333-3333-3333-3333-333333333333",
            "uri": "https://poliglot.io/os/spec#NoFlag",
            "value": "set",
        }

        variable = client._parse_variable(data)

        assert variable.has_value is True
        assert variable.required is False
        assert variable.variable_type is None
        assert variable.label is None
