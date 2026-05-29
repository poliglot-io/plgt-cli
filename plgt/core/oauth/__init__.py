"""OAuth authentication module.

This package provides OAuth authentication functionality for the Poliglot CLI.
"""

from plgt.core.oauth.client import OAuthClient
from plgt.core.oauth.errors import OAuthError
from plgt.core.oauth.utils import get_user_info

__all__ = ["OAuthClient", "OAuthError", "get_user_info"]
