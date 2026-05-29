"""Data types for secret management operations.

This module contains types for the secret management feature,
including the Secret dataclass for representing secret metadata.
"""

from dataclasses import dataclass
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
    has_value: bool
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    access_count: int
