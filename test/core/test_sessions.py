"""Unit tests for sessions module.

Tests cover APISession functionality including token refresh,
error handling, and request retry logic.
"""

import time
from unittest.mock import Mock, patch

import pytest
import requests
from plgt.core.exceptions import (
    AuthenticationError,
    ResourceNotFoundError,
    ServiceError,
    TokenRefreshedError,
    ValidationError,
)
from plgt.core.sessions import APISession


class TestAPISessionInitialization:
    """Test APISession initialization."""

    def test_init_without_profile(self):
        """Test initialization without authentication profile."""
        session = APISession()

        assert session.authenticated is False
        assert session.profile is None
        assert "Authorization" not in session.headers

    def test_init_with_profile(self):
        """Test initialization with authentication profile."""
        profile = {
            "access_token": "test-token-123",
            "refresh_token": "refresh-456",
            "expires_at": time.time() + 3600,
        }

        session = APISession(profile=profile)

        assert session.authenticated is True
        assert session.profile == profile
        assert session.headers["Authorization"] == "Bearer test-token-123"

    def test_init_with_api_key(self):
        """Test initialization with API key (non-interactive mode)."""
        profile = {"api_key": "api-key-xyz"}

        session = APISession(profile=profile)

        assert session.authenticated is True
        assert session.profile == profile
        assert session._is_api_key_mode is True
        assert session.headers["X-Api-Key"] == "api-key-xyz"
        assert "Authorization" not in session.headers

    def test_init_with_profile_missing_access_token(self):
        """Test initialization with profile missing access_token is unauthenticated."""
        # Profile exists but lacks access_token - could happen if config is corrupted
        profile = {"refresh_token": "refresh-123", "expires_at": "1234567890"}

        session = APISession(profile=profile)

        # Should be marked unauthenticated since we can't make requests without access_token
        assert session.authenticated is False
        assert session.profile == profile
        assert "Authorization" not in session.headers


class TestAPISessionTokenExpiry:
    """Test token expiry detection."""

    def test_is_token_expired_no_profile(self):
        """Test expiry check when no profile exists."""
        session = APISession()

        assert session._is_token_expired() is False

    def test_is_token_expired_no_expiry_field(self):
        """Test expiry check when expires_at field missing."""
        profile = {"access_token": "test-token"}
        session = APISession(profile=profile)

        assert session._is_token_expired() is False

    def test_is_token_expired_future_expiry(self):
        """Test token that expires in the future."""
        profile = {
            "access_token": "test-token",
            "expires_at": time.time() + 3600,  # Expires in 1 hour
        }
        session = APISession(profile=profile)

        assert session._is_token_expired() is False

    def test_is_token_expired_soon(self):
        """Test token that expires within 5 minutes."""
        profile = {
            "access_token": "test-token",
            "expires_at": time.time()
            + 200,  # Expires in 200 seconds (< 300s threshold)
        }
        session = APISession(profile=profile)

        assert session._is_token_expired() is True

    def test_is_token_expired_past(self):
        """Test token that already expired."""
        profile = {
            "access_token": "test-token",
            "expires_at": time.time() - 100,  # Expired 100 seconds ago
        }
        session = APISession(profile=profile)

        assert session._is_token_expired() is True

    def test_is_token_expired_invalid_timestamp(self):
        """Test handling of invalid expires_at value."""
        profile = {
            "access_token": "test-token",
            "expires_at": "invalid",
        }
        session = APISession(profile=profile)

        assert session._is_token_expired() is False  # Graceful fallback

    def test_is_token_expired_api_key_mode(self):
        """Test that API key mode never expires."""
        profile = {"api_key": "api-key-xyz"}
        session = APISession(profile=profile)

        assert session._is_token_expired() is False


class TestAPISessionTokenRefresh:
    """Test token refresh functionality."""

    def test_refresh_token_success(self):
        """Test successful token refresh."""
        profile = {
            "access_token": "old-token",
            "refresh_token": "refresh-123",
        }
        session = APISession(profile=profile)

        # Mock auth components
        mock_config = Mock()
        mock_oauth = Mock()
        new_tokens = {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 3600,
        }
        mock_oauth.refresh_access_token.return_value = new_tokens

        with patch.object(
            session, "_get_auth_components", return_value=(mock_config, mock_oauth)
        ):
            result = session._try_refresh_token()

        assert result is True
        assert session.profile["access_token"] == "new-token"
        assert session.headers["Authorization"] == "Bearer new-token"
        mock_config._update_credentials.assert_called_once_with(new_tokens)

    def test_refresh_token_no_refresh_token(self):
        """Test refresh fails when no refresh token available."""
        profile = {"access_token": "old-token"}  # No refresh_token
        session = APISession(profile=profile)

        result = session._try_refresh_token()

        assert result is False

    def test_refresh_token_no_profile(self):
        """Test refresh fails when no profile exists."""
        session = APISession()

        result = session._try_refresh_token()

        assert result is False

    def test_refresh_token_oauth_error(self):
        """Test refresh fails on OAuth error."""
        profile = {
            "access_token": "old-token",
            "refresh_token": "refresh-123",
        }
        session = APISession(profile=profile)

        # Mock auth components with OAuth client that raises error
        mock_config = Mock()
        mock_oauth = Mock()
        mock_oauth.refresh_access_token.side_effect = requests.RequestException(
            "OAuth failed"
        )

        with patch.object(
            session, "_get_auth_components", return_value=(mock_config, mock_oauth)
        ):
            result = session._try_refresh_token()

        assert result is False
        # Original profile should be unchanged
        assert session.profile["access_token"] == "old-token"


class TestAPISessionRequest:
    """Test HTTP request handling with token refresh."""

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    def test_request_success(self, mock_settings, mock_super_request):
        """Test successful request."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        profile = {"access_token": "test-token", "expires_at": time.time() + 3600}
        session = APISession(profile=profile)

        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_super_request.return_value = mock_response

        response = session.request("GET", "/api/test")

        assert response == mock_response
        mock_super_request.assert_called_once()
        # Verify full URL was constructed
        call_args = mock_super_request.call_args
        assert call_args[0][1] == "https://api.example.com/api/test"

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    def test_request_proactive_refresh(self, mock_settings, mock_super_request):
        """Test proactive token refresh when token expiring soon."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        profile = {
            "access_token": "old-token",
            "refresh_token": "refresh-123",
            "expires_at": time.time() + 100,  # Expires in 100s (< 300s threshold)
        }
        session = APISession(profile=profile)

        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_super_request.return_value = mock_response

        with patch.object(
            session, "_try_refresh_token", return_value=True
        ) as mock_refresh:
            response = session.request("GET", "/api/test")

            # Should have attempted refresh
            mock_refresh.assert_called_once()
            assert response == mock_response

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    def test_request_timeout_error(self, mock_settings, mock_super_request):
        """Test request timeout handling."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        session = APISession()
        mock_super_request.side_effect = requests.exceptions.Timeout("Timeout")

        with pytest.raises(ServiceError, match="Request timed out"):
            session.request("GET", "/api/test")

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    def test_request_too_many_redirects(self, mock_settings, mock_super_request):
        """Test too many redirects handling."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        session = APISession()
        mock_super_request.side_effect = requests.exceptions.TooManyRedirects(
            "Redirects"
        )

        with pytest.raises(ServiceError, match="Too many redirects"):
            session.request("GET", "/api/test")


class TestAPISessionErrorHandling:
    """Test HTTP error handling and retry logic."""

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    def test_request_401_triggers_refresh_retry(
        self, mock_settings, mock_super_request
    ):
        """Test 401 error triggers token refresh and retry."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        profile = {
            "access_token": "old-token",
            "refresh_token": "refresh-123",
        }
        session = APISession(profile=profile)

        # First request returns 401
        mock_401_response = Mock()
        mock_401_response.ok = False
        mock_401_response.status_code = 401
        mock_401_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_401_response
        )

        # Second request (after refresh) succeeds
        mock_success_response = Mock()
        mock_success_response.ok = True
        mock_success_response.status_code = 200

        mock_super_request.side_effect = [mock_401_response, mock_success_response]

        with patch.object(session, "_try_refresh_token", return_value=True):
            # Mock _handle_http_error to raise TokenRefreshedError
            with patch.object(
                session,
                "_handle_http_error",
                side_effect=TokenRefreshedError("Refreshed"),
            ):
                response = session.request("GET", "/api/test")

                # Should have retried after refresh
                assert mock_super_request.call_count == 2
                assert response == mock_success_response

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    @patch("plgt.core.sessions.APISession._handle_http_error")
    def test_request_400_validation_error(
        self, mock_handle_error, mock_settings, mock_super_request
    ):
        """Test 400 error handling."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        session = APISession()

        mock_400_response = Mock()
        mock_400_response.ok = False
        mock_400_response.status_code = 400
        mock_400_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_400_response
        )
        mock_super_request.return_value = mock_400_response

        # Mock _handle_http_error to raise ValidationError
        mock_handle_error.side_effect = ValidationError("Validation failed")

        with pytest.raises(ValidationError):
            session.request("POST", "/api/test")

    @patch.object(requests.Session, "request")
    @patch("plgt.core.sessions.settings")
    @patch("plgt.core.sessions.APISession._handle_http_error")
    def test_request_403_permission_denied(
        self, mock_handle_error, mock_settings, mock_super_request
    ):
        """Test 403 error handling."""
        mock_settings.PLATFORM_URL = "https://api.example.com"

        session = APISession()

        mock_403_response = Mock()
        mock_403_response.ok = False
        mock_403_response.status_code = 403
        mock_403_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_403_response
        )
        mock_super_request.return_value = mock_403_response

        # Mock _handle_http_error to raise ServiceError
        mock_handle_error.side_effect = ServiceError("Permission denied")

        with pytest.raises(ServiceError):
            session.request("GET", "/api/test")


class TestAPISessionBrowserLogin:
    """Test browser login functionality."""

    def test_browser_login_fails_in_api_key_mode(self):
        """Test that browser login fails in API key mode."""
        profile = {"api_key": "api-key-xyz"}
        session = APISession(profile=profile)

        result = session._initiate_browser_login()

        assert result is False

    @patch("sys.stdin.isatty")
    def test_browser_login_fails_in_non_interactive(self, mock_isatty):
        """Test that browser login fails in non-interactive environment."""
        mock_isatty.return_value = False
        profile = {"access_token": "test-token"}
        session = APISession(profile=profile)

        result = session._initiate_browser_login()

        assert result is False

    @patch("sys.stdin.isatty")
    def test_browser_login_success(self, mock_isatty):
        """Test successful browser login."""
        mock_isatty.return_value = True
        profile = {"access_token": "old-token"}
        session = APISession(profile=profile)

        # Mock auth components
        mock_config = Mock()
        mock_config._profile = {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
        }
        mock_oauth = Mock()

        with patch.object(
            session, "_get_auth_components", return_value=(mock_config, mock_oauth)
        ):
            result = session._initiate_browser_login()

        assert result is True
        assert session.profile["access_token"] == "new-token"
        assert session.headers["Authorization"] == "Bearer new-token"
        mock_oauth.auth_code_flow.assert_called_once()


class TestAPISessionHandleHTTPError:
    """Test HTTP error handler logic."""

    def test_handle_401_raises_token_refreshed(self):
        """Test that 401 errors raise TokenRefreshedError after refresh."""
        profile = {
            "access_token": "old-token",
            "refresh_token": "refresh-123",
        }
        session = APISession(profile=profile)

        mock_response = Mock()
        mock_response.status_code = 401
        error = requests.exceptions.HTTPError(response=mock_response)

        with patch.object(session, "_try_refresh_token", return_value=True):
            with pytest.raises(TokenRefreshedError):
                session._handle_http_error(error)

    def test_handle_401_raises_auth_error_when_refresh_fails(self):
        """Test that 401 raises AuthenticationError when refresh fails."""
        profile = {"access_token": "old-token"}  # No refresh token
        session = APISession(profile=profile)

        mock_response = Mock()
        mock_response.status_code = 401
        error = requests.exceptions.HTTPError(response=mock_response)

        with patch.object(session, "_initiate_browser_login", return_value=False):
            with pytest.raises(AuthenticationError, match="Authentication failed"):
                session._handle_http_error(error)

    def test_handle_400_validation_error_with_field_errors(self):
        """Test that 400 with field errors raises ValidationError with field details."""
        session = APISession()

        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "name": ["This field is required."],
        }
        error = requests.exceptions.HTTPError(response=mock_response)

        with pytest.raises(ValidationError, match="name: This field is required"):
            session._handle_http_error(error)

    def test_handle_400_validation_error_with_string_message(self):
        """Test that 400 with string message values raises ValidationError."""
        session = APISession()

        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"message": "Invalid input"}
        error = requests.exceptions.HTTPError(response=mock_response)

        with pytest.raises(ValidationError, match="message: Invalid input"):
            session._handle_http_error(error)

    def test_handle_404_not_found(self):
        """Test that 404 errors raise ResourceNotFoundError."""
        session = APISession()

        mock_response = Mock()
        mock_response.status_code = 404
        error = requests.exceptions.HTTPError(response=mock_response)

        with pytest.raises(ResourceNotFoundError, match="Not found"):
            session._handle_http_error(error)

    def test_handle_500_server_error(self):
        """Test that 500 errors raise ServiceError."""
        session = APISession()

        mock_response = Mock()
        mock_response.status_code = 500
        error = requests.exceptions.HTTPError(response=mock_response)

        with pytest.raises(ServiceError):
            session._handle_http_error(error)
