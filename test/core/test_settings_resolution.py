"""Unit tests for the dynamic resolution layer in ``plgt.core.settings``.

The resolution order (env > user config > hardcoded default) is the
contract that makes ``plgt`` portable across deployments without
rebuilding. These tests pin each tier so regressions are loud.
"""

from unittest.mock import MagicMock, patch

from plgt.core import settings


class TestPlatformUrlResolution:
    def test_env_var_wins_over_everything(self):
        fake_config = MagicMock()
        fake_config.defaults = {"base_url": "https://from-config.example.com"}

        with (
            patch.dict(
                "os.environ",
                {"POLIGLOT_BASE_URL": "https://from-env.example.com"},
                clear=False,
            ),
            patch.object(
                settings, "_DEFAULT_PLATFORM_URL", "https://built-in.example.com"
            ),
            patch("plgt.core._config.AppConfig", return_value=fake_config),
        ):
            with patch("plgt.core.config", fake_config):
                assert settings.platform_url() == "https://from-env.example.com"

    def test_config_wins_over_default(self):
        fake_config = MagicMock()
        fake_config.defaults = {"base_url": "https://from-config.example.com"}

        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(
                settings, "_DEFAULT_PLATFORM_URL", "https://built-in.example.com"
            ),
            patch("plgt.core.config", fake_config),
        ):
            import os

            os.environ.pop("POLIGLOT_BASE_URL", None)
            assert settings.platform_url() == "https://from-config.example.com"

    def test_default_when_nothing_configured(self):
        fake_config = MagicMock()
        fake_config.defaults = {}  # no base_url configured

        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(
                settings, "_DEFAULT_PLATFORM_URL", "https://built-in.example.com"
            ),
            patch("plgt.core.config", fake_config),
        ):
            import os

            os.environ.pop("POLIGLOT_BASE_URL", None)
            assert settings.platform_url() == "https://built-in.example.com"

    def test_dynamic_attribute_PLATFORM_URL_reflects_env(self):
        """``settings.PLATFORM_URL`` (capitalised) reads through ``__getattr__``."""
        with patch.dict(
            "os.environ",
            {"POLIGLOT_BASE_URL": "https://attr-env.example.com"},
            clear=False,
        ):
            assert settings.PLATFORM_URL == "https://attr-env.example.com"

    def test_oauth_urls_derive_from_resolved_platform_url(self):
        with patch.dict(
            "os.environ",
            {"POLIGLOT_BASE_URL": "https://urls.example.com"},
            clear=False,
        ):
            assert (
                settings.OAUTH2_AUTHORIZE_URL == "https://urls.example.com/oauth2/auth"
            )
            assert settings.OAUTH2_TOKEN_URL == "https://urls.example.com/oauth2/token"
            assert settings.OAUTH2_USER_INFO_URL == "https://urls.example.com/userinfo"


class TestOauthClientIdResolution:
    def test_returns_discovered_client_id(self):
        fake_config = MagicMock()
        fake_config.deployment = {"oauth_client_id": "discovered-client"}

        with patch("plgt.core.config", fake_config):
            assert settings.oauth_client_id() == "discovered-client"

    def test_returns_none_when_no_deployment_configured(self):
        """No build-time fallback — callers (auth flow) are expected to
        run ``discovery.ensure_deployment_configured()`` first."""
        fake_config = MagicMock()
        fake_config.deployment = {}

        with patch("plgt.core.config", fake_config):
            assert settings.oauth_client_id() is None

    def test_dynamic_OAUTH2_CLIENT_ID_reflects_deployment(self):
        fake_config = MagicMock()
        fake_config.deployment = {"oauth_client_id": "via-attr"}

        with patch("plgt.core.config", fake_config):
            assert settings.OAUTH2_CLIENT_ID == "via-attr"


class TestUnknownAttributeStillRaises:
    """``__getattr__`` must not swallow typos as silent ``None``."""

    def test_attribute_error_for_unknown_name(self):
        import pytest

        with pytest.raises(AttributeError, match="POLIGLOT_NONESUCH"):
            _ = settings.POLIGLOT_NONESUCH  # type: ignore[attr-defined]
