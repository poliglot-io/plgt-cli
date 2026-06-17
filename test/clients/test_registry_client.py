"""Unit tests for ``plgt.clients.registry_client``.

These tests inject the same exception types that ``APISession`` raises in
production. ``APISession.request`` converts 4xx/5xx HTTP responses into
``ResourceNotFoundError``/``ValidationError``/``ServiceError`` etc. before
``RegistryClient`` sees them, so a raw ``requests.exceptions.HTTPError``
never reaches the client. Mocking ``session.get`` with the wrong exception
type produced a false sense of coverage in earlier revisions; the side
effects below now match what the session actually raises.
"""

import hashlib
from unittest.mock import Mock

import pytest
import requests
from plgt.clients.registry_client import RegistryClient
from plgt.core.exceptions import ResourceNotFoundError, ServiceError


class TestGetVersions:
    def test_returns_versions_in_response_order(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {
                "publisher": "poliglot",
                "name": "os",
                "versions": [
                    {"version": "2.2.0", "publishedAt": "2026-04-01T00:00:00"},
                    {"version": "2.1.0", "publishedAt": "2026-03-01T00:00:00"},
                ],
            }
        }
        session.get.return_value = response

        client = RegistryClient(session)
        assert client.get_versions("poliglot", "os") == ["2.2.0", "2.1.0"]

        session.get.assert_called_once_with(
            "/api/v1/registry/packages/poliglot/os",
        )

    def test_unwraps_envelope_when_data_key_missing(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {"versions": [{"version": "1.0.0"}]}
        session.get.return_value = response

        client = RegistryClient(session)
        assert client.get_versions("poliglot", "os") == ["1.0.0"]

    def test_404_maps_to_resource_not_found(self) -> None:
        # APISession converts 404 to ResourceNotFoundError before the client sees the response.
        session = Mock()
        session.get.side_effect = ResourceNotFoundError("Not found.")

        client = RegistryClient(session)
        with pytest.raises(
            ResourceNotFoundError, match="Package not found in registry: ghost/package"
        ):
            client.get_versions("ghost", "package")

    def test_network_error_maps_to_service_error(self) -> None:
        session = Mock()
        session.get.side_effect = requests.exceptions.ConnectionError("boom")

        client = RegistryClient(session)
        with pytest.raises(ServiceError, match="Failed to fetch"):
            client.get_versions("poliglot", "os")


class TestGetDeclarations:
    def test_returns_typed_declarations(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {
                "declaredVariables": [
                    {
                        "uri": "https://example.com/crm#BatchSize",
                        "variableType": "xsd:integer",
                        "label": "Batch size",
                        "description": None,
                        "required": True,
                    }
                ],
                "declaredSecrets": [
                    {
                        "uri": "https://example.com/crm#ApiKey",
                        "label": "API key",
                        "description": "Widget service API key",
                        "required": True,
                    }
                ],
            }
        }
        session.get.return_value = response

        client = RegistryClient(session)
        variables, secrets = client.get_declarations("poliglot", "os", "2.1.0")

        assert len(variables) == 1
        assert variables[0].uri == "https://example.com/crm#BatchSize"
        assert variables[0].variable_type == "xsd:integer"
        assert variables[0].required is True

        assert len(secrets) == 1
        assert secrets[0].uri == "https://example.com/crm#ApiKey"
        assert secrets[0].required is True

        session.get.assert_called_once_with(
            "/api/v1/registry/packages/poliglot/os/2.1.0/declarations",
        )

    def test_empty_lists_when_unset(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {"data": {}}
        session.get.return_value = response

        client = RegistryClient(session)
        variables, secrets = client.get_declarations("poliglot", "os", "2.1.0")
        assert variables == []
        assert secrets == []

    def test_404_maps_to_resource_not_found(self) -> None:
        session = Mock()
        session.get.side_effect = ResourceNotFoundError("404")

        client = RegistryClient(session)
        with pytest.raises(
            ResourceNotFoundError,
            match=r"Version not found in registry: poliglot/os@9\.9\.9",
        ):
            client.get_declarations("poliglot", "os", "9.9.9")


class TestListCompatibleVersions:
    def test_returns_versions_with_engine_version(self) -> None:
        session = Mock()
        response = Mock()
        # Paged response: one page, items inside the envelope.
        response.json.return_value = {
            "data": {
                "items": [
                    {"version": "1.1.0", "engineVersion": ">=1 <2"},
                    {"version": "1.0.0", "engineVersion": ">=1 <2"},
                ],
                "currentPage": 0,
                "totalPages": 1,
                "totalResults": 2,
            }
        }
        session.get.return_value = response

        client = RegistryClient(session)
        result = client.list_compatible_versions("poliglot", "os")

        assert len(result) == 2
        assert result[0].version == "1.1.0"
        assert result[0].engine_version == ">=1 <2"
        assert result[1].version == "1.0.0"

        session.get.assert_called_once_with(
            "/api/v1/registry/packages/poliglot/os/versions",
            params={"page": "0", "size": "100"},
        )

    def test_passes_engine_version_filter(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {
                "items": [],
                "currentPage": 0,
                "totalPages": 0,
                "totalResults": 0,
            }
        }
        session.get.return_value = response

        client = RegistryClient(session)
        client.list_compatible_versions("poliglot", "os", engine_version=">=1 <2")

        session.get.assert_called_once_with(
            "/api/v1/registry/packages/poliglot/os/versions",
            params={"page": "0", "size": "100", "engineVersion": ">=1 <2"},
        )

    def test_pages_through_multi_page_results(self) -> None:
        # Dep resolver must see every compatible version. Verify the paging
        # loop actually fetches subsequent pages and concatenates items.
        session = Mock()
        page0 = Mock()
        page0.json.return_value = {
            "data": {
                "items": [{"version": "1.2.0", "engineVersion": ">=1 <2"}],
                "currentPage": 0,
                "totalPages": 2,
                "totalResults": 2,
            }
        }
        page1 = Mock()
        page1.json.return_value = {
            "data": {
                "items": [{"version": "1.0.0", "engineVersion": ">=1 <2"}],
                "currentPage": 1,
                "totalPages": 2,
                "totalResults": 2,
            }
        }
        session.get.side_effect = [page0, page1]

        client = RegistryClient(session)
        result = client.list_compatible_versions("poliglot", "os")

        assert [v.version for v in result] == ["1.2.0", "1.0.0"]
        assert session.get.call_count == 2

    def test_404_maps_to_resource_not_found(self) -> None:
        session = Mock()
        session.get.side_effect = ResourceNotFoundError("404")

        client = RegistryClient(session)
        with pytest.raises(
            ResourceNotFoundError,
            match="Package not found in registry: poliglot/unknown",
        ):
            client.list_compatible_versions("poliglot", "unknown")


class TestResolveNamespaceUri:
    def test_returns_resolution_with_versions(self) -> None:
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "data": {
                "uri": "urn:poliglot:registry:repository:widget/widget",
                "publisher": {
                    "uri": "urn:poliglot:registry:publisher:widget",
                    "slug": "widget",
                },
                "name": "widget",
                "versions": [
                    {"version": "1.2.0", "engineVersion": ">=1 <2"},
                ],
            }
        }
        session.get.return_value = response

        client = RegistryClient(session)
        result = client.resolve_namespace_uri(
            "https://example.com/spec/core#", engine_version=">=1 <2"
        )

        assert result is not None
        assert result.publisher_slug == "widget"
        assert result.name == "widget"
        assert len(result.versions) == 1
        assert result.versions[0].version == "1.2.0"

        session.get.assert_called_once_with(
            "/api/v1/registry/resolve",
            params={
                "uri": "https://example.com/spec/core#",
                "engineVersion": ">=1 <2",
            },
        )

    def test_returns_none_when_uri_unclaimed(self) -> None:
        session = Mock()
        # APISession converts 404 to ResourceNotFoundError before the client sees the response.
        session.get.side_effect = ResourceNotFoundError("404")

        client = RegistryClient(session)
        result = client.resolve_namespace_uri("https://unknown.example/spec#")
        assert result is None

    def test_5xx_errors_bubble_as_service_error(self) -> None:
        session = Mock()
        # APISession converts 5xx to ServiceError before the client sees the response. The
        # registry client doesn't intercept ServiceError, so it propagates unchanged.
        session.get.side_effect = ServiceError("Server error. Try again later.")

        client = RegistryClient(session)
        with pytest.raises(ServiceError):
            client.resolve_namespace_uri("https://example.com/spec/core#")


class TestDownloadArchive:
    def test_streams_to_destination_and_returns_checksum(self, tmp_path) -> None:
        # Server reports the SHA-256 of the bytes it sends; the client recomputes locally and
        # verifies the match before returning.
        body = b"chunk-onechunk-two"
        expected = f"sha256:{hashlib.sha256(body).hexdigest()}"

        session = Mock()
        response = Mock()
        response.headers = {"X-Archive-Checksum": expected}
        # Two chunks plus an empty chunk to confirm we skip empty ones.
        response.iter_content.return_value = iter([b"chunk-one", b"chunk-two", b""])
        session.get.return_value = response

        destination = tmp_path / "deps" / "poliglot" / "os" / "1.0.0" / "package.tgz"

        client = RegistryClient(session)
        checksum = client.download_archive("poliglot", "os", "1.0.0", destination)

        assert checksum == expected
        assert destination.exists()
        assert destination.read_bytes() == body
        # tmp file is renamed away, not left behind.
        assert not destination.with_suffix(destination.suffix + ".tmp").exists()
        response.close.assert_called_once()

        session.get.assert_called_once_with(
            "/api/v1/registry/packages/poliglot/os/1.0.0/archive",
            stream=True,
        )

    def test_fails_on_checksum_mismatch(self, tmp_path) -> None:
        session = Mock()
        response = Mock()
        response.headers = {"X-Archive-Checksum": "sha256:deadbeef"}
        response.iter_content.return_value = iter([b"unexpected-bytes"])
        session.get.return_value = response

        destination = tmp_path / "deps" / "poliglot" / "os" / "1.0.0" / "package.tgz"

        client = RegistryClient(session)
        with pytest.raises(ServiceError, match="Checksum mismatch"):
            client.download_archive("poliglot", "os", "1.0.0", destination)

        # Tamper-rejection: the partial download must not be left on disk.
        assert not destination.exists()
        assert not destination.with_suffix(destination.suffix + ".tmp").exists()

    def test_fails_when_checksum_header_missing(self, tmp_path) -> None:
        session = Mock()
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = iter([b"some-bytes"])
        session.get.return_value = response

        destination = tmp_path / "deps" / "poliglot" / "os" / "1.0.0" / "package.tgz"

        client = RegistryClient(session)
        with pytest.raises(ServiceError, match="missing X-Archive-Checksum"):
            client.download_archive("poliglot", "os", "1.0.0", destination)

        assert not destination.exists()

    def test_cleans_up_tmp_on_404(self, tmp_path) -> None:
        session = Mock()
        session.get.side_effect = ResourceNotFoundError("404")

        destination = tmp_path / "deps" / "poliglot" / "os" / "9.9.9" / "package.tgz"

        client = RegistryClient(session)
        with pytest.raises(
            ResourceNotFoundError,
            match=r"Version not found in registry: poliglot/os@9\.9\.9",
        ):
            client.download_archive("poliglot", "os", "9.9.9", destination)

        assert not destination.exists()
        assert not destination.with_suffix(destination.suffix + ".tmp").exists()
