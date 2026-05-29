import logging
import sys
import time
from typing import TYPE_CHECKING, Any

import requests

from . import settings
from .exceptions import (
    AuthenticationError,
    ConflictError,
    ResourceNotFoundError,
    ServiceError,
    TokenRefreshedError,
    ValidationError,
)

if TYPE_CHECKING:
    from plgt.core._config import AppConfig
    from plgt.core.oauth import OAuthClient

logger = logging.getLogger(settings.APP_AUTHOR)


class APISession(requests.Session):
    authenticated: bool
    profile: dict[str, Any] | None
    _is_api_key_mode: bool

    def __init__(
        self,
        profile: dict[str, Any] | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.authenticated = profile is not None
        self.profile = profile
        self._is_api_key_mode = False

        if profile:
            # Check if using API key mode (non-interactive)
            if "api_key" in profile:
                self._is_api_key_mode = True
                self.headers.update({"X-Api-Key": profile["api_key"]})
            elif "access_token" in profile:
                self.headers.update(
                    {"Authorization": f"Bearer {profile['access_token']}"}
                )
            else:
                # Profile exists but lacks access_token - mark as unauthenticated
                self.authenticated = False
                logger.debug("Profile missing access_token, session unauthenticated")

    def _get_auth_components(self) -> tuple["AppConfig", "OAuthClient"]:
        """Get authentication components (uses singleton config instance)."""
        from plgt.core import config
        from plgt.core.oauth import OAuthClient

        return config, OAuthClient()

    def _is_token_expired(self) -> bool:
        """Check if access token is expired or will expire soon."""
        # API key mode doesn't expire
        if self._is_api_key_mode:
            return False

        if not self.profile or "expires_at" not in self.profile:
            return False  # No expiration data, rely on 401 detection

        try:
            expires_at = int(self.profile["expires_at"])
            # Refresh if expires within 5 minutes (300 seconds)
            return time.time() >= (expires_at - 300)
        except (ValueError, TypeError):
            return False

    def request(self, method, url, *args, **kwargs) -> requests.Response:
        """Make an HTTP request with automatic token refresh and retry logic."""
        full_url = f"{settings.PLATFORM_URL}{url}"

        # Proactively refresh if token is expired/expiring soon
        if self._is_token_expired():
            if not self._try_refresh_token():
                if not self._initiate_browser_login():
                    # Refresh token itself is dead AND browser flow couldn't recover. Give the
                    # publisher the actionable next step instead of the platform's verbatim
                    # "Access token is not active" — they don't have a way to act on that.
                    msg = (
                        "Your session has expired and could not be refreshed. "
                        "Run `plgt auth login` to sign in again."
                    )
                    raise AuthenticationError(msg)

        try:
            response = super().request(method, full_url, *args, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as e:
            msg = "Request timed out."
            raise ServiceError(msg) from e
        except requests.exceptions.TooManyRedirects as e:
            msg = "Too many redirects."
            raise ServiceError(msg) from e
        except requests.exceptions.HTTPError as e:
            try:
                self._handle_http_error(e)
            except TokenRefreshedError:
                # Token was refreshed, retry the request once
                response = super().request(method, full_url, *args, **kwargs)
                response.raise_for_status()
                return response
            # _handle_http_error always raises, this is unreachable
            raise  # pragma: no cover

    def _initiate_browser_login(self) -> bool:
        """
        Initiate browser-based OAuth login flow.

        Returns:
            True if login succeeded, False otherwise
        """
        # Don't attempt browser login in API key mode
        if self._is_api_key_mode:
            logger.error("Cannot initiate browser login in API key mode")
            return False

        # Check if we're in an interactive environment
        if not sys.stdin.isatty():
            logger.error(
                "Cannot initiate browser login in non-interactive environment. "
                "Set POLIGLOT_API_KEY environment variable for non-interactive use."
            )
            return False

        try:
            logger.debug("Token refresh failed, initiating browser login...")

            config, oauth_client = self._get_auth_components()
            oauth_client.auth_code_flow()

            # Reload profile from config after login
            config._load_profile()  # noqa: SLF001 - Internal profile reload

            # Update session with new credentials
            self.profile = config._profile  # noqa: SLF001
            if self.profile and "access_token" in self.profile:
                self.headers.update(
                    {"Authorization": f"Bearer {self.profile['access_token']}"}
                )
                self.authenticated = True
                logger.debug("Browser login successful")
                return True

            return False

        except (requests.RequestException, ValueError, KeyError):
            logger.exception("Browser login failed")
            return False

    def _try_refresh_token(self) -> bool:
        """
        Attempt to refresh the access token.

        Returns:
            True if refresh succeeded, False otherwise
        """
        if not self.profile or "refresh_token" not in self.profile:
            return False

        try:
            config, oauth_client = self._get_auth_components()

            new_tokens = oauth_client.refresh_access_token(
                self.profile["refresh_token"]
            )

            # Update config with new tokens (silently)
            config._update_credentials(new_tokens)  # noqa: SLF001 - Internal credential update

            # Update session profile and headers
            self.profile.update(new_tokens)
            self.headers.update(
                {"Authorization": f"Bearer {new_tokens['access_token']}"}
            )

            return True

        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ):  # Token refresh errors
            return False

    def _handle_http_error(self, e: requests.exceptions.HTTPError):
        """Handle HTTP errors by raising appropriate custom exceptions."""
        if e.response.status_code == 400:
            self._handle_validation_error(e)
        elif e.response.status_code == 401:
            # Try to refresh token before failing
            if self._try_refresh_token():
                # Token refreshed, signal caller to retry
                msg = "Token refreshed, retry request"
                raise TokenRefreshedError(msg)

            # Refresh failed, try browser login
            logger.debug("Token refresh failed on 401, initiating browser login")
            if self._initiate_browser_login():
                # Browser login succeeded, signal caller to retry
                msg = "Browser login completed, retry request"
                raise TokenRefreshedError(msg)

            # Both refresh and browser login failed
            if self._is_api_key_mode:
                msg = (
                    "API key authentication failed. The key may be invalid or revoked."
                )
            else:
                msg = "Authentication failed. Unable to refresh token or complete browser login."
            raise AuthenticationError(msg) from e
        elif e.response.status_code == 403:
            msg = "You are not permitted to perform this action."
            raise AuthenticationError(msg) from e
        elif e.response.status_code == 404:
            msg = "Not found."
            raise ResourceNotFoundError(msg) from e
        elif e.response.status_code == 409:
            # Conflict - extract server error message and preserve raw body for
            # callers that want to inspect structured fields (e.g. install
            # attach behavior).
            msg = self._extract_api_error_message(e.response)
            body: dict | None = None
            try:
                parsed = e.response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except (ValueError, KeyError):
                body = None
            raise ConflictError(msg, body=body) from e
        elif 400 <= e.response.status_code < 500:
            # Other 4xx client errors - extract server error message
            msg = self._extract_api_error_message(e.response)
            raise ValidationError(msg) from e
        elif e.response.status_code >= 500:
            msg = "Server error. Try again later."
            raise ServiceError(msg) from e

    def _extract_api_error_message(self, response: requests.Response) -> str:
        """Extract error message from API response."""
        try:
            data = response.json()
            # Check for ApiResponse format with error field
            if isinstance(data, dict):
                if "error" in data and isinstance(data["error"], dict):
                    return data["error"].get("message", str(data))
                if "message" in data:
                    return data["message"]
            return str(data)
        except (ValueError, KeyError):
            # If JSON parsing fails, use status text
            return response.reason or f"HTTP {response.status_code}"

    def _handle_validation_error(self, e: requests.exceptions.HTTPError):
        """Handle 400 validation errors."""
        errors = e.response.json()

        if "detail" in errors and isinstance(errors["detail"], str):
            raise ValidationError(errors.get("detail")) from e
        if "non_field_errors" in errors:
            raise ValidationError(errors["non_field_errors"][0]) from e

        parts = []
        for field, field_errors in errors.items():
            if isinstance(field_errors, list):
                parts.extend(f"{field}: {err}" for err in field_errors)
            else:
                parts.append(f"{field}: {field_errors}")
        message = "\n".join(parts) if parts else str(errors)
        raise ValidationError(message) from e
