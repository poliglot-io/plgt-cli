import logging

from plgt.core import settings

logger = logging.getLogger(settings.APP_AUTHOR)


class CLIError(Exception):
    exit_code: int = 1


class AuthenticationError(CLIError):
    exit_code: int = 2


class ValidationError(CLIError):
    exit_code: int = 3


class ServiceError(CLIError):
    exit_code: int = 4


class ResourceNotFoundError(ServiceError):
    pass


class ConflictError(ValidationError):
    """Raised when the server returns 409 Conflict.

    Carries the parsed response body so callers (e.g. install attach handling)
    can read structured fields like ``conflict``, ``existing``, ``requested``.
    Subclass of ``ValidationError`` to preserve backwards compatibility with
    callers that catch ``ValidationError`` for "active operation" 409s.
    """

    def __init__(self, message: str, body: dict | None = None) -> None:
        super().__init__(message)
        self.body = body or {}


class ProfileDoesNotExistError(AuthenticationError):
    pass


class TokenRefreshedError(Exception):
    """Raised internally when a token is refreshed to signal retry."""


class UserInfoError(AuthenticationError):
    """Error retrieving user information from API."""


class PortUnavailableError(ServiceError):
    """No available port found for local server."""
