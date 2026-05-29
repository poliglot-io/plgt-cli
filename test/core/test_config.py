"""Unit tests for _config module.

Tests cover AppConfig configuration management.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from plgt.core._config import AppConfig


class TestAppConfigInit:
    """Test AppConfig initialization."""

    @patch("plgt.core._config.settings")
    def test_init_creates_config_file(self, mock_settings):
        """Test initialization creates config file if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config"
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            AppConfig()

            assert config_path.exists()

    @patch("plgt.core._config.settings")
    def test_init_creates_defaults_section(self, mock_settings):
        """Test initialization creates defaults section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()

            assert config.has_section("defaults")


class TestAppConfigInstanceIsolation:
    """Test AppConfig instances are properly isolated."""

    @patch("plgt.core._config.settings")
    def test_instances_have_separate_profiles(self, mock_settings):
        """Test that each AppConfig instance has its own _profile dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config1 = AppConfig()
            config2 = AppConfig()

            # Modify one instance's profile
            config1._profile["test_key"] = "test_value"

            # Should not affect the other instance
            assert "test_key" not in config2._profile

    @patch("plgt.core._config.settings")
    def test_instances_have_separate_sessions(self, mock_settings):
        """Test that each AppConfig instance has its own _session object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config1 = AppConfig()
            config2 = AppConfig()

            # Should be different session objects
            assert config1._session is not config2._session


class TestAppConfigDefaults:
    """Test defaults property and operations."""

    @patch("plgt.core._config.settings")
    def test_defaults_property_returns_dict(self, mock_settings):
        """Test defaults property returns dictionary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()
            config.set("defaults", "workspace", "test-ws")

            defaults = config.defaults

            assert isinstance(defaults, dict)
            assert defaults.get("workspace") == "test-ws"

    @patch("plgt.core._config.settings")
    def test_set_defaults(self, mock_settings):
        """Test setting default values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()
            config.set_defaults(workspace="my-workspace")

            assert config.get("defaults", "workspace") == "my-workspace"


class TestAppConfigDeployment:
    """Test [deployment] section persistence."""

    @patch("plgt.core._config.settings")
    def test_deployment_property_empty_when_no_section(self, mock_settings):
        """Fresh config returns an empty dict — never KeyErrors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()

            assert config.deployment == {}

    @patch("plgt.core._config.settings")
    def test_set_deployment_persists_all_fields(self, mock_settings):
        """All discovered fields are written under [deployment] and survive
        a reload from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()
            config.set_deployment(
                deployment_name="acme",
                deployment_version="2.4.0",
                oauth_issuer="https://acme/oauth",
                oauth_client_id="plgt-cli-acme",
                discovered_at="2026-05-26T00:00:00+00:00",
                min_cli_version="0.1.0",
            )

            # Reload from disk to confirm persistence.
            reloaded = AppConfig()

            assert reloaded.deployment["deployment_name"] == "acme"
            assert reloaded.deployment["oauth_client_id"] == "plgt-cli-acme"
            assert reloaded.deployment["min_cli_version"] == "0.1.0"
            assert reloaded.deployment["discovered_at"] == "2026-05-26T00:00:00+00:00"

    @patch("plgt.core._config.settings")
    def test_set_deployment_replaces_existing_section(self, mock_settings):
        """Switching deployments wipes the stale section — no field leak."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()
            config.set_deployment(
                deployment_name="old",
                deployment_version="1.0.0",
                oauth_issuer="https://old/oauth",
                oauth_client_id="old-client",
                min_cli_version="0.0.1",
            )

            # New deployment has no min_cli_version — the old value must not survive.
            config.set_deployment(
                deployment_name="new",
                deployment_version="2.0.0",
                oauth_issuer="https://new/oauth",
                oauth_client_id="new-client",
            )

            assert config.deployment["deployment_name"] == "new"
            assert "min_cli_version" not in config.deployment

    @patch("plgt.core._config.settings")
    def test_set_deployment_skips_none_values(self, mock_settings):
        """``None`` fields are dropped — ConfigParser can't store None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_settings.CONFIG_ROOT = Path(tmpdir)

            config = AppConfig()
            config.set_deployment(
                deployment_name="x",
                deployment_version="1",
                oauth_issuer="https://x",
                oauth_client_id="cid",
                min_cli_version=None,
            )

            assert "min_cli_version" not in config.deployment
