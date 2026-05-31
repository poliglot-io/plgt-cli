"""Unit tests for configure command.

Tests cover configuration command functionality.
"""

from unittest.mock import patch

import pytest
import typer
from plgt.cmd.configure import _apply_base_url, defaults, refresh
from plgt.core.discovery import DeploymentMetadata
from plgt.core.exceptions import ServiceError, ValidationError


class TestDefaults:
    """Test defaults configuration command."""

    @patch("plgt.cmd.configure.config")
    @patch("plgt.cmd.configure.validators")
    def test_set_workspace(self, mock_validators, mock_config):
        """Test setting workspace default."""
        mock_validators.slug.return_value = True  # Valid workspace

        defaults(workspace="my-workspace", base_url=None)

        mock_config.set_defaults.assert_called_once()
        call_args = mock_config.set_defaults.call_args[1]
        assert call_args["workspace"] == "my-workspace"
        mock_config.save.assert_called_once()

    @patch("plgt.cmd.configure.config")
    def test_invalid_workspace_aborts(self, mock_config):
        """Test that invalid workspace causes abort."""
        # validators.slug will return False/ValidationError for invalid

        with pytest.raises(typer.Abort):
            defaults(workspace="invalid workspace!", base_url=None)

        mock_config.set_defaults.assert_not_called()


def _metadata(min_cli_version="0.0.1"):
    return DeploymentMetadata(
        deployment_name="acme",
        deployment_version="2.4.0",
        oauth_issuer="https://acme.example.com/oauth",
        oauth_client_id="plgt-cli-acme",
        min_cli_version=min_cli_version,
    )


class TestApplyBaseUrl:
    """Test the shared base-URL persistence path."""

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_persists_base_url_and_deployment(self, mock_config, mock_discover):
        mock_discover.return_value = _metadata()

        _apply_base_url("https://acme.example.com")

        mock_config.set_defaults.assert_called_once_with(
            base_url="https://acme.example.com"
        )
        mock_config.set_deployment.assert_called_once()
        deployment_kwargs = mock_config.set_deployment.call_args[1]
        assert deployment_kwargs["deployment_name"] == "acme"
        assert deployment_kwargs["oauth_client_id"] == "plgt-cli-acme"

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_strips_trailing_slash_before_persist(self, mock_config, mock_discover):
        mock_discover.return_value = _metadata()

        _apply_base_url("https://acme.example.com/")

        mock_config.set_defaults.assert_called_once_with(
            base_url="https://acme.example.com"
        )

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_accepts_localhost_base_url(self, mock_config, mock_discover):
        # validators.url is exercised for real here (only discover/config are
        # mocked) so this guards against the default validator rejecting
        # hostnames without a public TLD. Local dev points at http://localhost.
        mock_discover.return_value = _metadata()

        _apply_base_url("http://localhost:8080")

        mock_config.set_defaults.assert_called_once_with(
            base_url="http://localhost:8080"
        )
        mock_config.set_deployment.assert_called_once()

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_accepts_loopback_ip_base_url(self, mock_config, mock_discover):
        mock_discover.return_value = _metadata()

        _apply_base_url("http://127.0.0.1:8080")

        mock_config.set_defaults.assert_called_once_with(
            base_url="http://127.0.0.1:8080"
        )
        mock_config.set_deployment.assert_called_once()

    @patch("plgt.cmd.configure.config")
    def test_invalid_url_aborts(self, mock_config):
        with pytest.raises(typer.Abort):
            _apply_base_url("not-a-url")

        mock_config.set_defaults.assert_not_called()
        mock_config.set_deployment.assert_not_called()

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_discovery_failure_aborts_without_persist(self, mock_config, mock_discover):
        mock_discover.side_effect = ServiceError("could not reach")

        with pytest.raises(typer.Abort):
            _apply_base_url("https://acme.example.com")

        mock_config.set_defaults.assert_not_called()
        mock_config.set_deployment.assert_not_called()

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_min_cli_version_rejection_aborts(self, mock_config, mock_discover):
        # CLI is 0.1.0; require 99.0.0 to force rejection.
        mock_discover.return_value = _metadata(min_cli_version="99.0.0")

        with pytest.raises(typer.Abort):
            _apply_base_url("https://acme.example.com")

        mock_config.set_defaults.assert_not_called()
        mock_config.set_deployment.assert_not_called()

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_bad_discovery_payload_aborts(self, mock_config, mock_discover):
        # ValidationError from discover() (e.g. missing oauth) must also abort.
        mock_discover.side_effect = ValidationError("missing fields")

        with pytest.raises(typer.Abort):
            _apply_base_url("https://acme.example.com")

        mock_config.set_defaults.assert_not_called()


class TestDefaultsBaseUrl:
    """Test the --base-url flag on the defaults command."""

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_base_url_only_triggers_discovery(self, mock_config, mock_discover):
        mock_discover.return_value = _metadata()

        defaults(workspace=None, base_url="https://acme.example.com")

        mock_discover.assert_called_once_with("https://acme.example.com")
        mock_config.set_defaults.assert_called_once_with(
            base_url="https://acme.example.com"
        )
        mock_config.set_deployment.assert_called_once()

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.validators")
    @patch("plgt.cmd.configure.config")
    def test_base_url_with_workspace_persists_both(
        self, mock_config, mock_validators, mock_discover
    ):
        mock_validators.slug.return_value = True
        mock_validators.url.return_value = True
        mock_discover.return_value = _metadata()

        defaults(
            workspace="acme-ws",
            base_url="https://acme.example.com",
        )

        # First call: base_url. Second call: workspace.
        assert mock_config.set_defaults.call_count == 2
        first, second = mock_config.set_defaults.call_args_list
        assert first.kwargs == {"base_url": "https://acme.example.com"}
        assert second.kwargs == {"workspace": "acme-ws"}


class TestRefresh:
    """Test the refresh command."""

    @patch("plgt.cmd.configure.discover")
    @patch("plgt.cmd.configure.config")
    def test_refresh_uses_persisted_base_url(self, mock_config, mock_discover):
        mock_config.defaults = {"base_url": "https://acme.example.com"}
        mock_discover.return_value = _metadata()

        refresh()

        mock_discover.assert_called_once_with("https://acme.example.com")
        mock_config.set_deployment.assert_called_once()

    @patch("plgt.cmd.configure.config")
    def test_refresh_without_configured_base_url_aborts(self, mock_config):
        mock_config.defaults = {}  # no base_url

        with pytest.raises(typer.Abort):
            refresh()
