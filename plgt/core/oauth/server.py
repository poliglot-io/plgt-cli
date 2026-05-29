"""HTTP server components for OAuth callback handling."""

from __future__ import annotations

import urllib.parse as urlparse
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

from plgt.core.oauth.errors import OAuthError
from plgt.core.oauth.utils import get_error_param

if TYPE_CHECKING:
    from plgt.core.oauth.client import OAuthClient


class RequestHandlerWrapper:
    """Utility class to link the server and the request handler.

    This allows to kill the server from the request processing and
    pass state between the handler and the OAuth client.
    """

    oauth_client: OAuthClient
    complete: bool  # tells the server to stop listening to requests
    error_message: str | None  # error encountered while processing the callback

    def __init__(self, oauth_client: OAuthClient) -> None:
        """Initialize the wrapper.

        Args:
            oauth_client: OAuth client for processing callbacks
        """
        self.oauth_client = oauth_client
        self.complete = False
        self.error_message = None

    @property
    def request_handler(self) -> type[BaseHTTPRequestHandler]:
        """Create and return a RequestHandler class bound to this wrapper.

        The handler class is created dynamically to capture the wrapper
        instance in a closure, allowing the handler to communicate back
        to the wrapper.

        Returns:
            A BaseHTTPRequestHandler subclass
        """
        wrapper = self  # Capture the wrapper instance in the closure

        class RequestHandler(BaseHTTPRequestHandler):
            """HTTP handler for OAuth callback requests."""

            def do_GET(self) -> None:
                """Process GET requests for OAuth callback.

                Non-root requests are skipped. If an authorization code can
                be extracted from the URI, processes the callback and signals
                the server to stop.
                """
                callback_url: str = self.path
                parsed_url = urlparse.urlparse(callback_url)

                if parsed_url.path == "/":
                    error_string = get_error_param(parsed_url)
                    if error_string is not None:
                        self._end_request(200)
                        wrapper.error_message = (
                            wrapper.oauth_client.get_server_error_message(error_string)
                        )
                    else:
                        try:
                            wrapper.oauth_client.process_callback(callback_url)
                        except OAuthError as error:
                            self._end_request(400)
                            wrapper.error_message = error.message
                        else:
                            # Redirect to consent complete page on success
                            self._end_request(
                                301,
                                f"{wrapper.oauth_client._dist_url}/auth/consent/complete",  # noqa: SLF001
                            )

                    # Signal the server to stop
                    wrapper.complete = True
                else:
                    self._end_request(404)

            def _end_request(
                self,
                status_code: int,
                redirect_url: str | None = None,
            ) -> None:
                """End the current request with optional redirect.

                Args:
                    status_code: HTTP status code to send
                    redirect_url: Optional URL for redirect response
                """
                self.send_response(status_code)
                if redirect_url is not None:
                    self.send_header("Location", redirect_url)
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                """Silence log messages."""

        return RequestHandler
