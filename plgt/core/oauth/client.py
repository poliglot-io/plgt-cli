"""OAuth client for authentication flow."""

import json
import logging
import time
import urllib.parse as urlparse
import webbrowser
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import HTTPServer
from typing import Any

import requests
import typer
from oauthlib.oauth2 import OAuth2Error, WebApplicationClient
from requests import Session

from plgt.core import config, settings
from plgt.core.discovery import ensure_deployment_configured
from plgt.core.exceptions import PortUnavailableError, UserInfoError
from plgt.core.oauth.errors import OAuthError
from plgt.core.oauth.server import RequestHandlerWrapper
from plgt.core.oauth.utils import get_user_info

logger = logging.getLogger(settings.APP_AUTHOR)


class OAuthClient:
    """Helper class to handle the OAuth authentication flow.

    The logic is divided in 2 steps:
    - Open the browser on Poliglot login screen and run a local server to wait for callback
    - Handle the oauth callback to exchange an authorization code against a valid access token
    """

    def __init__(self):
        """Initialize the OAuth client.

        Lazily populates ``[deployment]`` metadata if the user hasn't run
        ``plgt configure defaults`` yet — discovery against
        ``settings.platform_url()`` produces the ``oauth_client_id`` and
        related identifiers we need to start the flow.
        """
        ensure_deployment_configured()

        self._dist_url = settings.PLATFORM_URL
        self._oauth_client = WebApplicationClient(settings.OAUTH2_CLIENT_ID)
        self._state = ""  # use the `state` property instead

        self._session = Session()

        self._handler_wrapper = RequestHandlerWrapper(oauth_client=self)

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: str | None = None

        self._port = settings.USABLE_PORT_RANGE[0]
        self.server: HTTPServer | None = None

        self._generate_pkce_pair()

    @property
    def redirect_uri(self) -> str:
        """Return the redirect URI for OAuth callback."""
        return f"http://localhost:{self._port}"

    @property
    def state(self) -> str:
        """Return the state used to verify the auth process.

        The state is included in the redirect_uri and is expected in the callback url.
        Then, if both states don't match, the process fails.
        The state is an url-encoded string dict containing the token name.
        It is cached to prevent from altering its value during the process.
        """
        if not self._state:
            self._state = urlparse.quote(json.dumps({"token_name": self._token_name}))
        return self._state

    def _check_existing_token(self) -> bool:
        """Check if the config already has a credential set and can get the user info object.

        If one could be found, outputs a message including the expiry date
        and return True. Else return False.
        """
        try:
            token = config.get_access_token(self._token_name)
            if token is None:
                return False
            return bool(get_user_info(token))
        except (UserInfoError, requests.RequestException, KeyError, ValueError):
            return False

    def _generate_pkce_pair(self) -> None:
        """Generate a code verifier and its sha encoded version for PKCE."""
        self.code_verifier = self._oauth_client.create_code_verifier(43)
        self.code_challenge = (
            urlsafe_b64encode(sha256(self.code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

    def _redirect_to_login(self) -> None:
        """Open the user's browser to poliglot.io login."""
        request_uri = self._oauth_client.prepare_request_uri(
            uri=settings.OAUTH2_AUTHORIZE_URL,
            redirect_uri=self.redirect_uri,
            scope=settings.OAUTH2_SCOPES,
            code_challenge=self.code_challenge,
            code_challenge_method="S256",
            state=self.state,
        )
        logger.info(
            "[blue]Complete the login process in your browser:\n\n[link=%s]Open web app[/link][/blue]\n\n",
            request_uri,
        )
        webbrowser.open_new_tab(request_uri)

    def _prepare_server(self) -> None:
        """Prepare the local HTTP server for OAuth callback."""
        for port in range(*settings.SERVER_PORT_RANGE):
            try:
                self.server = HTTPServer(
                    ("localhost", port),
                    self._handler_wrapper.request_handler,
                )
                self._port = port
                break
            except OSError:
                continue
        else:
            msg = "Could not find unoccupied port."
            raise PortUnavailableError(msg)

    def _wait_for_callback(self) -> None:
        """Wait to receive and process the authorization callback on the local server.

        This catches HTTP requests made on the previously opened server.
        The callback processing logic is implemented in the request handler class.
        """
        try:
            while not self._handler_wrapper.complete:
                self.server.handle_request()  # type: ignore
        except KeyboardInterrupt:
            raise typer.Abort from None

        if self._handler_wrapper.error_message is not None:
            raise OAuthError(self._handler_wrapper.error_message) from None

    def _get_authorization_code(self, uri: str) -> str:
        """Extract the authorization code from the callback URI.

        Args:
            uri: The callback URI containing the authorization code

        Returns:
            The extracted authorization code

        Raises:
            OAuthError: If no code can be extracted or the state is invalid
        """
        try:
            authorization_code = self._oauth_client.parse_request_uri_response(
                uri,
                self.state,
            ).get("code")
        except OAuth2Error:
            authorization_code = None

        if authorization_code is None:
            msg = "Invalid code or state received from the callback."
            raise OAuthError(msg)
        return authorization_code

    def _claim_token(self, authorization_code: str) -> None:
        """Exchange the authorization code for an access token.

        Args:
            authorization_code: The authorization code to exchange

        Raises:
            OAuthError: If no valid token could be retrieved
        """
        request_body = self._oauth_client.prepare_request_body(
            code=authorization_code,
            redirect_uri=self.redirect_uri,
            code_verifier=self.code_verifier,
        )

        response = self._session.post(
            settings.OAUTH2_TOKEN_URL,
            request_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        raise_error = False
        if response.ok:
            try:
                response_json = response.json()
                self._access_token = response_json["access_token"]
                self._refresh_token = response_json["refresh_token"]
                self._expires_in = response_json.get("expires_in", 3600)
            except (json.decoder.JSONDecodeError, ValueError):
                raise_error = True
        else:
            raise_error = True

        if raise_error:
            msg = "Cannot create a token."
            raise OAuthError(msg)

    def _validate_access_token(self) -> tuple[str, str | None, dict[str, Any]]:
        """Validate the token and get user info.

        Returns:
            Tuple of (access_token, refresh_token, user_info)
        """
        assert self._access_token is not None
        return (
            self._access_token,
            self._refresh_token,
            get_user_info(self._access_token),
        )

    def _save_token(
        self,
        access_token: str,
        refresh_token: str | None,
        userinfo: dict[str, Any],
    ) -> None:
        """Save the new token in the configuration with expiration timestamp.

        Args:
            access_token: OAuth access token
            refresh_token: OAuth refresh token (may be None)
            userinfo: User information from the provider
        """
        assert access_token is not None
        assert refresh_token is not None
        assert userinfo is not None

        expires_in = getattr(self, "_expires_in", 3600)
        expires_at = int(time.time()) + expires_in

        creds = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": str(expires_at),
        }

        config.login(creds)

    def refresh_access_token(self, refresh_token: str) -> dict[str, str]:
        """Refresh access token using refresh token.

        Args:
            refresh_token: The refresh token from previous authentication

        Returns:
            dict with new access_token and refresh_token

        Raises:
            Exception if refresh fails
        """
        logger.debug("Refreshing access token...")

        token_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.OAUTH2_CLIENT_ID,
        }

        response = requests.post(settings.OAUTH2_TOKEN_URL, data=token_data)
        response.raise_for_status()

        tokens = response.json()
        expires_in = tokens.get("expires_in", 3600)
        expires_at = int(time.time()) + expires_in

        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", refresh_token),
            "expires_at": str(expires_at),
        }

    def auth_code_flow(self) -> None:
        """Handle the whole oauth process.

        This includes:
        - Opening the user's webbrowser to Poliglot login page
        - Opening a server and waiting for the callback processing
        """
        self._token_name = "profile"

        if self._check_existing_token():
            return

        self._prepare_server()
        self._redirect_to_login()
        self._wait_for_callback()

    def process_callback(self, callback_url: str) -> None:
        """Process the OAuth callback.

        This function runs within the request handler do_GET method and:
        - Extracts the authorization code
        - Exchanges the code against an access token
        - Validates the new token
        - Saves the token in configuration

        Args:
            callback_url: The callback URL with authorization code

        Raises:
            OAuthError: If any step in the process fails
        """
        authorization_code = self._get_authorization_code(callback_url)
        self._claim_token(authorization_code)
        token_data = self._validate_access_token()
        self._save_token(*token_data)

    def get_server_error_message(self, error_code: str) -> str:
        """Return the human readable message for the given error code.

        Args:
            error_code: The error code from the server

        Returns:
            Human readable error message
        """
        return f"An unknown server error has occurred (error code: {error_code})."
