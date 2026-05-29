"""OAuth utility functions."""

import urllib.parse as urlparse
from typing import Any

import requests

from plgt.core import settings
from plgt.core.exceptions import UserInfoError


def get_error_param(parsed_url: urlparse.ParseResult) -> str | None:
    """Extract the value of the 'error' url param.

    Args:
        parsed_url: Parsed URL to extract error from

    Returns:
        Error string if present, None otherwise
    """
    params = urlparse.parse_qs(parsed_url.query)
    if "error" in params:
        return params["error"][0]
    return None


def get_user_info(access_token: str) -> dict[str, Any]:
    """Return the user info object from the OAuth provider.

    Args:
        access_token: The OAuth access token

    Returns:
        User information dictionary from the provider

    Raises:
        UserInfoError: If the user info request fails
    """
    response = requests.get(
        settings.OAUTH2_USER_INFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )

    if response.ok:
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError as e:
            msg = f"Invalid JSON response from user info endpoint. Status: {response.status_code}, Content: {response.text[:200]}"
            raise UserInfoError(msg) from e

    msg = f"Failed to get user info. Status: {response.status_code}, Content: {response.text[:200]}"
    raise UserInfoError(msg) from None
