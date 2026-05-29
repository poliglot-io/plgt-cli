"""Default CLI settings.

URL- and OAuth-related values resolve dynamically per call rather than as
import-time module constants. There is no build-time substitution and no
packaged ``config.yaml`` — defaults are hardcoded here and overridden at
runtime via env vars or the user config in ``~/.plgt/config``.

Resolution order for ``platform_url()``:

    POLIGLOT_BASE_URL env
      > [defaults] base_url in ~/.plgt/config
      > _DEFAULT_PLATFORM_URL

Resolution order for ``oauth_client_id()``:

    [deployment] oauth_client_id in ~/.plgt/config

When ``[deployment]`` is empty, callers should run discovery
(``plgt.core.discovery.discover``) against the active ``platform_url()``
to populate it. Auth flows do this lazily on first use; an explicit
``plgt configure defaults [--base-url <url>]`` can also be used to switch
deployments.

The ``PLATFORM_URL``, ``OAUTH2_AUTHORIZE_URL``, ``OAUTH2_TOKEN_URL``,
``OAUTH2_USER_INFO_URL`` and ``OAUTH2_CLIENT_ID`` names are preserved as
module-level dynamic attributes (via ``__getattr__``) so callers continue
to write ``settings.PLATFORM_URL`` and get the value resolved at the time
of access.
"""

import os
from pathlib import Path

from appdirs import user_cache_dir

# App settings

APP_NAME = "Poliglot CLI"

APP_VERSION = "0.1.0"

APP_AUTHOR = "poliglot"

# Networking

USABLE_PORT_RANGE = [1024, 30000]

# Directory settings

APP_DIR = Path(__file__).parent.parent

CACHE_DIR = user_cache_dir(APP_NAME, APP_AUTHOR)

CONFIG_DIR = Path.home() / ".plgt"

RESOURCE_DIR = APP_DIR / "resources"

TEMPLATE_DIR = APP_DIR / "templates"

CONFIG_ROOT = Path(CONFIG_DIR)

CACHE_ROOT = Path(CACHE_DIR)

CONFIG_ROOT.mkdir(parents=True, exist_ok=True)

CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# Application context

PROJECT_DIR = Path.cwd() / ".matrix"

SERVER_PORT_RANGE = (29170, 29998)

# OAuth2

# We will always have to allow insecure transport to support the local callback api
# This is secure because of the locality of the requests
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = str(True)

OAUTH2_SCOPES = ("openid", "offline")

# Hardcoded defaults. The dynamic ``platform_url()`` accessor layers env
# vars and the user config on top.
_DEFAULT_PLATFORM_URL = "https://poliglot.io"


def platform_url() -> str:
    """Resolve the active platform base URL.

    Order: ``POLIGLOT_BASE_URL`` env > ``[defaults] base_url`` in the user
    config > ``_DEFAULT_PLATFORM_URL``. The user-config lookup is
    import-late to avoid a circular import (``_config`` imports settings).
    """
    env = os.environ.get("POLIGLOT_BASE_URL")
    if env:
        return env

    # Late import: ``plgt.core._config`` imports settings at module load.
    try:
        from plgt.core import config

        configured = config.defaults.get("base_url")
        if configured:
            return configured
    except (ImportError, AttributeError):
        # During settings import the config singleton may not exist yet;
        # fall through to the default.
        pass

    return _DEFAULT_PLATFORM_URL


def oauth_client_id() -> str | None:
    """Resolve the active OAuth client_id from the user config.

    Returns ``None`` if ``[deployment]`` has not been populated yet.
    Auth flows should call ``plgt.core.discovery.discover`` against
    ``platform_url()`` to populate it on first use, then re-read this.
    """
    try:
        from plgt.core import config

        return config.deployment.get("oauth_client_id")
    except (ImportError, AttributeError):
        return None


# UI Kit — invocation that hands off to the @poliglot-io/uikit npm package.
UIKIT_COMMAND = ["npx", "-p", "@poliglot-io/uikit", "poliglot-ui"]


# Module-level dynamic attributes. Keeping the historical UPPER_SNAKE names
# means existing call sites (``settings.PLATFORM_URL``) and existing test
# patterns (``mock_settings.PLATFORM_URL = "https://..."``) keep working
# without churn. Each access re-runs the resolver, so per-test env / config
# changes take effect immediately.
_DYNAMIC_ATTRS = {
    "PLATFORM_URL": lambda: platform_url(),
    "OAUTH2_AUTHORIZE_URL": lambda: f"{platform_url()}/oauth2/auth",
    "OAUTH2_TOKEN_URL": lambda: f"{platform_url()}/oauth2/token",
    "OAUTH2_USER_INFO_URL": lambda: f"{platform_url()}/userinfo",
    "OAUTH2_CLIENT_ID": oauth_client_id,
}


def __getattr__(name: str):
    """Resolve dynamic settings on attribute access."""
    if name in _DYNAMIC_ATTRS:
        return _DYNAMIC_ATTRS[name]()
    msg = f"module 'plgt.core.settings' has no attribute {name!r}"
    raise AttributeError(msg)
