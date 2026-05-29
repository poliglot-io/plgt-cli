"""Unit tests for deployment discovery.

These tests intercept the bare ``requests.get`` call that
``plgt.core.discovery.discover`` issues. We do not bring in a third-party
mocking lib because no other client in this repo needs one; the existing
test pattern (Mock'd session/response) carries here too, just one level
lower (we mock ``requests.get`` since discovery does not run through
``APISession``).
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import requests
from plgt.core.discovery import (
    WELL_KNOWN_PATH,
    DeploymentMetadata,
    _version_tuple,
    discover,
    enforce_min_cli_version,
)
from plgt.core.exceptions import ServiceError, ValidationError


def _make_response(payload, *, ok=True, status_code=200):
    """Build a Mock with the subset of ``requests.Response`` discovery uses."""
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    if isinstance(payload, ValueError):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    return response


_VALID_PAYLOAD = {
    "deployment_name": "acme-prod",
    "deployment_version": "2.4.0",
    "oauth": {
        "issuer": "https://acme.example.com/oauth2",
        "openid_configuration": "https://acme.example.com/.well-known/openid-configuration",
        "cli_client_id": "plgt-cli-acme",
        "cli_scopes": ["openid", "offline"],
    },
    "min_cli_version": "0.1.0",
}


class TestDiscover:
    @patch("plgt.core.discovery.requests.get")
    def test_returns_parsed_metadata_on_success(self, mock_get):
        mock_get.return_value = _make_response(_VALID_PAYLOAD)

        metadata = discover("https://acme.example.com")

        assert isinstance(metadata, DeploymentMetadata)
        assert metadata.deployment_name == "acme-prod"
        assert metadata.deployment_version == "2.4.0"
        assert metadata.oauth_issuer == "https://acme.example.com/oauth2"
        assert metadata.oauth_client_id == "plgt-cli-acme"
        assert metadata.min_cli_version == "0.1.0"

    @patch("plgt.core.discovery.requests.get")
    def test_hits_well_known_path(self, mock_get):
        mock_get.return_value = _make_response(_VALID_PAYLOAD)

        discover("https://acme.example.com")

        called_url = mock_get.call_args[0][0]
        assert called_url == f"https://acme.example.com{WELL_KNOWN_PATH}"

    @patch("plgt.core.discovery.requests.get")
    def test_strips_trailing_slash_from_base_url(self, mock_get):
        mock_get.return_value = _make_response(_VALID_PAYLOAD)

        discover("https://acme.example.com/")

        called_url = mock_get.call_args[0][0]
        assert called_url == f"https://acme.example.com{WELL_KNOWN_PATH}"

    def test_empty_base_url_raises_validation(self):
        with pytest.raises(ValidationError, match="base_url is required"):
            discover("")

    @patch("plgt.core.discovery.requests.get")
    def test_network_error_raises_service_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("boom")

        with pytest.raises(ServiceError, match="Could not reach deployment"):
            discover("https://acme.example.com")

    @patch("plgt.core.discovery.requests.get")
    def test_non_2xx_raises_service_error(self, mock_get):
        mock_get.return_value = _make_response({}, ok=False, status_code=404)

        with pytest.raises(ServiceError, match="did not publish a discovery document"):
            discover("https://acme.example.com")

    @patch("plgt.core.discovery.requests.get")
    def test_non_json_response_raises_validation(self, mock_get):
        mock_get.return_value = _make_response(ValueError("not json"))

        with pytest.raises(ValidationError, match="non-JSON discovery document"):
            discover("https://acme.example.com")

    @patch("plgt.core.discovery.requests.get")
    def test_missing_oauth_object_raises_validation(self, mock_get):
        payload = {
            "deployment_name": "x",
            "deployment_version": "1",
            # no 'oauth'
        }
        mock_get.return_value = _make_response(payload)

        with pytest.raises(
            ValidationError, match="missing the required 'oauth' object"
        ):
            discover("https://acme.example.com")

    @patch("plgt.core.discovery.requests.get")
    def test_missing_required_field_lists_field_name(self, mock_get):
        payload = {
            "deployment_name": "acme",
            # missing deployment_version
            "oauth": {
                "issuer": "https://acme/oauth",
                "cli_client_id": "id",
            },
        }
        mock_get.return_value = _make_response(payload)

        with pytest.raises(ValidationError, match="deployment_version"):
            discover("https://acme.example.com")

    @patch("plgt.core.discovery.requests.get")
    def test_min_cli_version_optional(self, mock_get):
        payload = dict(_VALID_PAYLOAD)
        del payload["min_cli_version"]
        mock_get.return_value = _make_response(payload)

        metadata = discover("https://acme.example.com")

        assert metadata.min_cli_version is None

    @patch("plgt.core.discovery.requests.get")
    def test_unknown_top_level_keys_are_ignored(self, mock_get):
        # Forward-compat: the platform must be free to add new top-level keys
        # without bumping the minimum CLI version. An older CLI parsing a newer
        # response should ignore unknown keys rather than raise.
        payload = dict(_VALID_PAYLOAD)
        payload["future_capability"] = {"some_flag": True}
        mock_get.return_value = _make_response(payload)

        metadata = discover("https://acme.example.com")

        assert metadata.deployment_name == "acme-prod"


class TestToConfigDict:
    def test_includes_discovered_at_in_iso_utc(self):
        metadata = DeploymentMetadata(
            deployment_name="x",
            deployment_version="1",
            oauth_issuer="https://x/oauth",
            oauth_client_id="cid",
            min_cli_version="0.0.1",
        )

        result = metadata.to_config_dict()

        assert "discovered_at" in result
        # Parse round-trips and is timezone-aware UTC.
        parsed = datetime.fromisoformat(result["discovered_at"])
        assert parsed.tzinfo is not None
        assert parsed.tzinfo.utcoffset(parsed) == timezone.utc.utcoffset(parsed)

    def test_omits_min_cli_version_when_none(self):
        metadata = DeploymentMetadata(
            deployment_name="x",
            deployment_version="1",
            oauth_issuer="https://x/oauth",
            oauth_client_id="cid",
            min_cli_version=None,
        )

        assert "min_cli_version" not in metadata.to_config_dict()


class TestEnforceMinCliVersion:
    def _make(self, *, min_cli_version):
        return DeploymentMetadata(
            deployment_name="x",
            deployment_version="1",
            oauth_issuer="https://x/oauth",
            oauth_client_id="cid",
            min_cli_version=min_cli_version,
        )

    @patch("plgt.core.discovery.settings")
    def test_accepts_matching_version(self, mock_settings):
        mock_settings.APP_VERSION = "0.1.0"

        # No raise.
        enforce_min_cli_version(self._make(min_cli_version="0.1.0"))

    @patch("plgt.core.discovery.settings")
    def test_accepts_newer_cli(self, mock_settings):
        mock_settings.APP_VERSION = "0.2.0"

        enforce_min_cli_version(self._make(min_cli_version="0.1.0"))

    @patch("plgt.core.discovery.settings")
    def test_rejects_older_cli_with_actionable_message(self, mock_settings):
        mock_settings.APP_VERSION = "0.0.5"

        with pytest.raises(ValidationError, match="older than the minimum"):
            enforce_min_cli_version(self._make(min_cli_version="0.1.0"))

    def test_no_min_version_no_op(self):
        # Should not raise even with an absurd APP_VERSION.
        enforce_min_cli_version(self._make(min_cli_version=None))


class TestVersionTuple:
    def test_parses_simple_dotted(self):
        assert _version_tuple("1.2.3") == (1, 2, 3)

    def test_handles_two_components(self):
        assert _version_tuple("1.2") == (1, 2)

    def test_strips_prerelease_suffix(self):
        assert _version_tuple("1.2.3-beta.1") == (1, 2, 3)

    def test_strips_build_metadata(self):
        assert _version_tuple("1.2.3+build5") == (1, 2, 3)

    def test_stops_at_non_numeric(self):
        # ``1.0.dev0`` is sometimes produced by setuptools-scm; treat as
        # the same precision as ``1.0``.
        assert _version_tuple("1.0.dev0") == (1, 0)
