"""Data types for variable management operations.

This module contains types for the variable management feature,
including the Variable dataclass for representing a workspace variable
and its current value.
"""

from dataclasses import dataclass


@dataclass
class Variable:
    """A workspace variable from the Platform API.

    Variables are declared by matrices and carry a plaintext value
    (unlike secrets, whose values are E2E-encrypted). A variable may be
    ``required`` or optional; an optional variable can be cleared by
    setting its value to ``None``.
    """

    id: str
    uri: str
    value: str | None
    has_value: bool
    variable_type: str | None
    label: str | None
    required: bool
