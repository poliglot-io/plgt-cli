import contextlib
import logging
import os
from configparser import ConfigParser, DuplicateSectionError
from configparser import Error as ConfigParserError
from pathlib import Path
from typing import Any

from plgt.core import sessions, settings
from plgt.core.decorators import clitask
from plgt.core.exceptions import AuthenticationError, ValidationError

logger = logging.getLogger(settings.APP_AUTHOR)


class AppConfig(ConfigParser):
    file_path: Path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Instance-level attributes (not class-level) to avoid shared state
        self._session: sessions.APISession = sessions.APISession()
        self._profile: dict[str, str] = {}
        self._workspace: dict[str, Any] = {}

        self.file_path = settings.CONFIG_ROOT / "config"

        if not self.has_section("defaults"):
            self.add_section("defaults")

        if not Path(self.file_path).exists():
            self.save()

        self.read(self.file_path)

        try:
            self._load_profile()
        except (ConfigParserError, KeyError, ValueError, OSError):
            logger.debug("Failed to load profile from config", exc_info=True)

    @property
    def defaults(self) -> dict[str, Any]:  # type: ignore[override]
        """Get default configuration values."""
        return dict(self.items("defaults"))

    @property
    def deployment(self) -> dict[str, str]:
        """Get the cached deployment metadata.

        Populated by ``plgt configure defaults --base-url <url>`` and
        ``plgt configure refresh`` from the
        ``{base_url}/.well-known/poliglot.json`` discovery endpoint.
        Empty dict if no custom base URL has been configured (the SaaS
        default path).
        """
        if not self.has_section("deployment"):
            return {}
        return dict(self.items("deployment"))

    @property
    def profile(self) -> dict[str, str]:
        """Get the current user profile."""
        return self._profile

    @property
    def workspace(self) -> dict[str, Any]:
        """Get the current workspace configuration."""
        return self._workspace

    def _load_profile(self):
        """Retrieve a profile as a dictionary"""
        # Check for API key first (non-interactive environments)
        api_key = os.environ.get("POLIGLOT_API_KEY")
        if api_key:
            self._profile = {"api_key": api_key}
            self._session = sessions.APISession(self._profile)
            return

        section = "profile"

        if not self.has_section(section):
            return

        self._profile = dict(self.items(section))

        self._session = sessions.APISession(self._profile)

    def get_access_token(self, token_name: str = "profile") -> str | None:  # noqa: S107
        """
        Get the access token from the configuration.

        Args:
            token_name: Name of the token section (default: "profile")

        Returns:
            The access token string, or None if not found
        """
        # Check for API key first
        api_key = os.environ.get("POLIGLOT_API_KEY")
        if api_key:
            return api_key

        # Check for stored OAuth token
        if self.has_section(token_name):
            profile = dict(self.items(token_name))
            return profile.get("access_token")

        return None

    def ensure_fresh_token(self) -> str | None:
        """
        Ensure the access token is fresh, refreshing if expired.

        Checks token expiry and attempts to refresh if needed.
        May trigger browser login in interactive environments if refresh fails.

        Returns:
            Fresh access token, or None if not authenticated

        Raises:
            AuthenticationError: If token is expired and cannot be refreshed
        """
        # API key mode - no expiry
        api_key = os.environ.get("POLIGLOT_API_KEY")
        if api_key:
            return api_key

        # Check if we have a session
        if not self._session or not self._session.authenticated:
            return None

        # Use session's expiry check and refresh logic
        if self._session._is_token_expired():  # noqa: SLF001
            # Try to refresh
            if not self._session._try_refresh_token():  # noqa: SLF001
                # Refresh failed, try browser login
                logger.debug("Token refresh failed, attempting browser login")
                if not self._session._initiate_browser_login():  # noqa: SLF001
                    msg = "Authentication failed. Token expired and could not be refreshed."
                    raise AuthenticationError(msg)

        return self._profile.get("access_token")

    def get_session(self) -> sessions.APISession:
        """Retrieve the current user session."""
        if not self._session.authenticated:
            self._load_profile()

        return self._session

    def add_workspace(self, slug: str, workspace_id: str, description: str):
        """Add a workspace to the config."""
        section = f"workspace {slug}"

        if not self.has_section(section):
            self.add_section(section)

        self.set(section, "id", workspace_id)
        self.set(section, "description", description)

        self.save()

        logger.info("Added workspace '%s'.", slug)

    def set_workspace(self, workspace: str):
        """Set the active workspace."""
        logger.info("Using workspace '%s'", workspace)

        section = f"workspace {workspace}"

        if not self.has_section(section):
            message = f"Could not find workspace '{workspace}' in config. Run 'plgt auth sync'."
            raise ValidationError(message)

        self._workspace = {**dict(self.items(section)), "slug": workspace}

        self.save()

    def set_defaults(self, **kwargs):
        section = "defaults"

        for k, v in kwargs.items():
            self.set(section, k, v)

        self.save()

    def set_deployment(self, **kwargs):
        """Persist deployment-discovery metadata under the ``[deployment]`` section.

        All values are coerced to strings (ConfigParser stores text). Replaces
        the entire section so stale fields from a previous deployment never
        leak into a newly-configured one.
        """
        section = "deployment"

        if self.has_section(section):
            self.remove_section(section)
        self.add_section(section)

        for k, v in kwargs.items():
            if v is None:
                continue
            self.set(section, k, str(v))

        self.save()

    def _update_credentials(self, credentials: dict[str, Any]):
        """Internal method to update credentials without logging."""
        key = "profile"

        if not self.has_section(key):
            self.add_section(key)

        for k, v in credentials.items():
            self.set(key, k, v)

        with contextlib.suppress(DuplicateSectionError):
            self.add_section("defaults")

        self.save()

    @clitask(action="Saving log in credentials", max_retries=0)
    def login(self, credentials: dict[str, Any]):
        """Add a new credential set to the persisted config file."""
        self._update_credentials(credentials)

    def save(self) -> None:
        """Save the current configuration."""
        with Path(self.file_path).open("w+") as configfile:
            self.write(configfile)
