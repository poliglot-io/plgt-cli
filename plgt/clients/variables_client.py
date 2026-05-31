"""Variables API client for Platform Service integration.

This module provides a client for interacting with the variables API
endpoints. Variable values are plaintext (unlike secrets, which use E2E
encryption), so the client is a thin wrapper over the REST contract.
"""

import logging

from plgt.core.sessions import APISession
from plgt.models.variable import Variable

logger = logging.getLogger(__name__)


class VariablesClient:
    """Client for variables API operations."""

    def __init__(self, session: APISession):
        """Initialize the variables client with an API session.

        Args:
            session: Authenticated API session for making requests.
        """
        self.session = session

    def list_variables(self, workspace: str) -> list[Variable]:
        """List all variables in a workspace.

        Args:
            workspace: The workspace slug.

        Returns:
            List of Variable objects with metadata and current values.
        """
        logger.debug("Listing variables in workspace %s", workspace)

        response = self.session.get(f"/api/v1/variables/{workspace}")

        data = response.json()

        # Handle API response wrapper.
        if "data" in data:
            data = data["data"]

        # Handle PagedResponse wrapper.
        if isinstance(data, dict) and "items" in data:
            data = data["items"]

        variables_data = data if isinstance(data, list) else []
        return [self._parse_variable(v) for v in variables_data]

    def set_variable_value(
        self,
        workspace: str,
        variable_id: str,
        value: str | None,
    ) -> Variable:
        """Set (or clear) a variable's value.

        Args:
            workspace: The workspace slug.
            variable_id: The variable's UUID.
            value: The value to set. Pass ``None`` to clear the variable.

        Returns:
            The updated Variable.
        """
        logger.debug(
            "Setting variable value %s in workspace %s",
            variable_id,
            workspace,
        )

        response = self.session.put(
            f"/api/v1/variables/{workspace}/{variable_id}/value",
            json={"value": value},
        )

        data = response.json()

        # Handle API response wrapper.
        if "data" in data:
            data = data["data"]

        return self._parse_variable(data)

    def _parse_variable(self, data: dict) -> Variable:
        """Parse variable data from an API response.

        Args:
            data: Raw variable data from the API.

        Returns:
            Variable object.
        """
        return Variable(
            id=data["id"],
            uri=data["uri"],
            value=data.get("value"),
            has_value=data.get("hasValue", data.get("value") is not None),
            variable_type=data.get("variableType"),
            label=data.get("label"),
            required=data.get("required", False),
        )
