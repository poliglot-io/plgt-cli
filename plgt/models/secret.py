"""Data types for secret management operations.

This module contains types for the secret management feature,
including the Secret dataclass for representing secret metadata.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Secret:
    """Secret metadata from the Platform API.

    Represents a secret's metadata without its value.
    The value must be fetched separately with E2E encryption.
    """

    id: str
    uri: str
    description: str
    created_at: datetime
    updated_at: datetime
    # Scopes at which a value may be set for this secret (e.g. workspace- or
    # principal-scoped). Empty when the API reports none.
    allowed_scopes: list[str] = field(default_factory=list)
    # The matrix that declares this secret, carrying its uri and (when resolved)
    # human-readable name. Either may be ``None`` when the API does not resolve it.
    matrix_uri: str | None = None
    matrix_name: str | None = None
