"""Unit tests for `plgt.services.bindings`.

Covers declaration discovery from project matrix TTL files, prefix/QName
resolution, flag parsing, TTY/non-TTY prompt behavior, and secret
encryption via the `/pubkey` flow.
"""

from __future__ import annotations

import base64
import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest
from plgt.core.exceptions import ServiceError, ValidationError
from plgt.models.build_types import MatrixBuildConfig, PackageConfig
from plgt.services.bindings import (
    EncryptedSecretBinding,
    ProjectDeclarations,
    RegistryDeclarations,
    SecretBinding,
    SecretDeclaration,
    VariableBinding,
    VariableDeclaration,
    collect_bindings,
    discover_project_declarations,
    encrypt_secret_bindings,
    parse_secret_from_env_flag,
    parse_var_flag,
    resolve_ref,
    resolve_registry_ref,
)

DEMO_TTL = textwrap.dedent(
    """
    @prefix rdf:        <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs:       <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd:        <http://www.w3.org/2001/XMLSchema#> .
    @prefix plgt-mtx:   <https://poliglot.io/os/spec/matrix#> .
    @prefix plgt-build: <https://poliglot.io/os/spec/build#> .
    @prefix plgt-scrt:  <https://poliglot.io/os/spec/secrets#> .
    @prefix demo:       <https://example.com/demo#> .

    demo: a plgt-mtx:Matrix ;
          plgt-mtx:name "demo" .

    demo:ApiBase a plgt-build:Variable ;
          plgt-build:variableType xsd:string ;
          plgt-build:label "API base URL" ;
          plgt-build:description "Where the demo service lives" ;
          plgt-build:required "true"^^xsd:boolean ;
          rdfs:isDefinedBy demo: .

    demo:OptionalFlag a plgt-build:Variable ;
          plgt-build:variableType xsd:boolean ;
          plgt-build:required "false"^^xsd:boolean ;
          rdfs:isDefinedBy demo: .

    demo:ApiKey a plgt-scrt:ManagedSecret ;
          plgt-scrt:label "API key" ;
          plgt-scrt:description "Bearer token" ;
          plgt-scrt:required "true"^^xsd:boolean ;
          rdfs:isDefinedBy demo: .

    demo:OptionalSecret a plgt-scrt:ManagedSecret ;
          plgt-scrt:required "false"^^xsd:boolean ;
          rdfs:isDefinedBy demo: .
    """
)


def _make_project(tmp_path: Path, ttl: str = DEMO_TTL) -> PackageConfig:
    """Build a tiny on-disk project with one matrix and the supplied TTL."""
    matrix_dir = tmp_path / "demo"
    spec_dir = matrix_dir / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "demo.ttl").write_text(ttl)
    return PackageConfig(
        name="demo",
        version="0.0.1",
        engine_version=">=1 <2",
        project_dir=tmp_path,
        matrices=[
            MatrixBuildConfig(
                name="demo",
                path=Path("demo"),
                spec_patterns=["./spec"],
                artifact_patterns=[],
                output_dir=Path("./.matrix"),
            )
        ],
    )


class TestDiscoverProjectDeclarations:
    def test_extracts_variables_and_secrets(self, tmp_path: Path) -> None:
        decls = discover_project_declarations(_make_project(tmp_path))

        assert {v.uri for v in decls.variables} == {
            "https://example.com/demo#ApiBase",
            "https://example.com/demo#OptionalFlag",
        }
        assert {s.uri for s in decls.secrets} == {
            "https://example.com/demo#ApiKey",
            "https://example.com/demo#OptionalSecret",
        }

        api_base = next(v for v in decls.variables if v.uri.endswith("#ApiBase"))
        assert api_base.required is True
        assert api_base.variable_type == "http://www.w3.org/2001/XMLSchema#string"
        assert api_base.label == "API base URL"

        optional_flag = next(
            v for v in decls.variables if v.uri.endswith("#OptionalFlag")
        )
        assert optional_flag.required is False

    def test_captures_prefix_declarations(self, tmp_path: Path) -> None:
        decls = discover_project_declarations(_make_project(tmp_path))

        assert decls.prefixes["demo"] == "https://example.com/demo#"
        assert decls.prefixes["plgt-build"] == "https://poliglot.io/os/spec/build#"

    def test_filters_out_resources_not_defined_by_project_matrices(
        self, tmp_path: Path
    ) -> None:
        # `imported:Foo` declares isDefinedBy pointing OUTSIDE this project's
        # matrices and must NOT be reported.
        ttl = DEMO_TTL + textwrap.dedent(
            """
            @prefix imported: <https://other.example.com/imported#> .

            imported:Foo a plgt-build:Variable ;
                  plgt-build:variableType xsd:string ;
                  rdfs:isDefinedBy imported: .
            """
        )
        decls = discover_project_declarations(_make_project(tmp_path, ttl))

        assert all(
            not v.uri.startswith("https://other.example.com/") for v in decls.variables
        )

    def test_no_declarations_returns_empty(self, tmp_path: Path) -> None:
        # Bare matrix with no variables/secrets.
        ttl = textwrap.dedent(
            """
            @prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .
            @prefix demo: <https://example.com/demo#> .
            demo: a plgt-mtx:Matrix ; plgt-mtx:name "demo" .
            """
        )
        decls = discover_project_declarations(_make_project(tmp_path, ttl))
        assert decls.variables == []
        assert decls.secrets == []


class TestParseFlags:
    def test_parse_var_flag_splits_on_first_equals(self) -> None:
        ref, val = parse_var_flag("crm:Foo=bar=baz")
        assert ref == "crm:Foo"
        assert val == "bar=baz"

    def test_parse_var_flag_missing_equals_fails(self) -> None:
        with pytest.raises(ValidationError, match="REF=VALUE"):
            parse_var_flag("crm:Foo")

    def test_parse_secret_from_env_flag(self) -> None:
        ref, env_var = parse_secret_from_env_flag("crm:ApiKey=ENV_VAR")
        assert ref == "crm:ApiKey"
        assert env_var == "ENV_VAR"

    def test_parse_secret_from_env_flag_missing_env_var(self) -> None:
        with pytest.raises(ValidationError, match="empty ENV_VAR"):
            parse_secret_from_env_flag("crm:ApiKey=")


class TestResolveRef:
    PREFIXES = {"crm": "https://example.com/crm#"}

    def test_full_uri_returned_as_is(self) -> None:
        uri = "https://example.com/crm#SyncBatchSize"
        assert resolve_ref(uri, self.PREFIXES) == uri

    def test_qname_resolved_against_prefixes(self) -> None:
        assert (
            resolve_ref("crm:SyncBatchSize", self.PREFIXES)
            == "https://example.com/crm#SyncBatchSize"
        )

    def test_undeclared_prefix_fails_with_clear_error(self) -> None:
        with pytest.raises(ValidationError, match="prefix 'unknown'"):
            resolve_ref("unknown:Foo", self.PREFIXES)

    def test_ref_without_colon_or_uri_scheme_fails(self) -> None:
        with pytest.raises(ValidationError, match="full URI or"):
            resolve_ref("Foo", self.PREFIXES)


class TestCollectBindings:
    def _decls(self) -> ProjectDeclarations:
        return ProjectDeclarations(
            variables=[
                VariableDeclaration(
                    uri="https://example.com/crm#SyncBatchSize",
                    variable_type="http://www.w3.org/2001/XMLSchema#integer",
                    label="Sync Batch Size",
                    description="Records per sync batch",
                    required=True,
                ),
                VariableDeclaration(
                    uri="https://example.com/crm#Optional",
                    variable_type="http://www.w3.org/2001/XMLSchema#string",
                    label=None,
                    description=None,
                    required=False,
                ),
            ],
            secrets=[
                SecretDeclaration(
                    uri="https://example.com/crm#ApiKey",
                    label="API Key",
                    description=None,
                    required=True,
                ),
            ],
            prefixes={"crm": "https://example.com/crm#"},
        )

    def test_flags_satisfy_required_no_prompt_needed(self) -> None:
        decls = self._decls()
        prompt = Mock(return_value="should-not-be-called")

        var_bindings, secret_bindings = collect_bindings(
            decls,
            ["crm:SyncBatchSize=100"],
            ["crm:ApiKey=MY_ENV"],
            is_tty=True,
            prompt=prompt,
            env={"MY_ENV": "supersecret"},
        )

        prompt.assert_not_called()
        assert var_bindings == [
            VariableBinding(uri="https://example.com/crm#SyncBatchSize", value="100")
        ]
        assert secret_bindings == [
            SecretBinding(uri="https://example.com/crm#ApiKey", value="supersecret")
        ]

    def test_full_uri_form_resolves_identically_to_qname(self) -> None:
        decls = self._decls()

        var_bindings, secret_bindings = collect_bindings(
            decls,
            ["https://example.com/crm#SyncBatchSize=100"],
            ["https://example.com/crm#ApiKey=MY_ENV"],
            is_tty=False,
            env={"MY_ENV": "supersecret"},
        )

        assert var_bindings[0].uri == "https://example.com/crm#SyncBatchSize"
        assert secret_bindings[0].uri == "https://example.com/crm#ApiKey"

    def test_prompts_for_missing_required_in_tty(self) -> None:
        decls = self._decls()
        prompt = Mock(side_effect=["100", "supersecret"])

        var_bindings, secret_bindings = collect_bindings(
            decls,
            [],
            [],
            is_tty=True,
            prompt=prompt,
            env={},
        )

        # Two required declarations are prompted; the optional one is left
        # to flags and is not prompted.
        assert prompt.call_count == 2
        var_call, secret_call = prompt.call_args_list
        assert var_call.kwargs["hide_input"] is False
        assert secret_call.kwargs["hide_input"] is True
        assert var_bindings[0].value == "100"
        assert secret_bindings[0].value == "supersecret"

    def test_skips_prompts_for_optional_declarations(self) -> None:
        decls = self._decls()
        # Required ones satisfied via flags; optional shouldn't be prompted.
        prompt = Mock(return_value="should-not-be-called")

        collect_bindings(
            decls,
            ["crm:SyncBatchSize=100"],
            ["crm:ApiKey=MY_ENV"],
            is_tty=True,
            prompt=prompt,
            env={"MY_ENV": "supersecret"},
        )
        prompt.assert_not_called()

    def test_empty_input_skips_required_binding(self) -> None:
        """Empty input at a required prompt skips the binding without raising.

        Bindings are optional at install time — the platform surfaces a
        warning event for any required declaration left unset rather than
        rejecting the install.
        """
        decls = self._decls()
        # User presses Enter on both required prompts.
        prompt = Mock(side_effect=["", ""])

        var_bindings, secret_bindings = collect_bindings(
            decls, [], [], is_tty=True, prompt=prompt, env={}
        )
        assert var_bindings == []
        assert secret_bindings == []

    def test_non_tty_missing_required_skips_silently(self) -> None:
        """Non-TTY mode skips all prompts and returns empty bindings.

        The platform treats every binding as optional at install time
        (warning-only on missing required), so the CLI no longer raises in
        CI. Required-but-unset bindings are surfaced via a warning summary
        rendered to the supplied console.
        """
        decls = self._decls()
        var_bindings, secret_bindings = collect_bindings(
            decls, [], [], is_tty=False, env={}
        )
        assert var_bindings == []
        assert secret_bindings == []

    def test_undeclared_qname_prefix_in_var_flag(self) -> None:
        decls = self._decls()
        with pytest.raises(ValidationError, match="prefix 'unknown'"):
            collect_bindings(
                decls,
                ["unknown:Foo=value"],
                [],
                is_tty=False,
                env={},
            )

    def test_unset_env_var_for_secret_flag(self) -> None:
        decls = self._decls()
        with pytest.raises(ValidationError, match="env var 'MISSING_VAR' is not set"):
            collect_bindings(
                decls,
                ["crm:SyncBatchSize=100"],
                ["crm:ApiKey=MISSING_VAR"],
                is_tty=False,
                env={},
            )

    def test_var_flag_uri_not_declared_fails(self) -> None:
        decls = self._decls()
        with pytest.raises(ValidationError, match="not declared"):
            collect_bindings(
                decls,
                ["https://example.com/crm#NotDeclared=value"],
                [],
                is_tty=False,
                env={},
            )

    def test_duplicate_var_flag_fails(self) -> None:
        decls = self._decls()
        with pytest.raises(ValidationError, match="more than once"):
            collect_bindings(
                decls,
                [
                    "crm:SyncBatchSize=100",
                    "https://example.com/crm#SyncBatchSize=200",
                ],
                ["crm:ApiKey=MY_ENV"],
                is_tty=False,
                env={"MY_ENV": "x"},
            )


class TestEncryptSecretBindings:
    def test_one_pubkey_call_per_secret_binding(self) -> None:
        # Build a server pubkey response that the `encrypt_secret_value`
        # helper can consume — generate a real X25519 keypair so encryption
        # actually succeeds.
        from nacl.public import PrivateKey

        server_priv = PrivateKey.generate()
        server_pub_b64 = base64.b64encode(bytes(server_priv.public_key)).decode("ascii")

        mock_session = Mock()

        def post_pubkey(*_args, **_kwargs):
            response = Mock()
            response.json.return_value = {
                "data": {
                    "serverPublicKey": server_pub_b64,
                    "keyId": "key-1",
                }
            }
            return response

        mock_session.post.side_effect = post_pubkey

        bindings = [
            SecretBinding(uri="https://example.com/crm#ApiKey", value="value-a"),
            SecretBinding(uri="https://example.com/crm#OtherKey", value="value-b"),
        ]

        encrypted = encrypt_secret_bindings(mock_session, "ws", bindings)

        assert len(encrypted) == 2
        # One /pubkey call per binding.
        assert mock_session.post.call_count == 2
        for esb in encrypted:
            assert isinstance(esb, EncryptedSecretBinding)
            assert esb.key_id == "key-1"
            # Sanity-check base64 encoding survived round-trip.
            base64.b64decode(esb.encrypted_value)
            base64.b64decode(esb.client_public_key)
            base64.b64decode(esb.nonce)

    def test_pubkey_request_failure_raises_service_error(self) -> None:
        mock_session = Mock()
        mock_session.post.side_effect = RuntimeError("network down")

        with pytest.raises(ServiceError, match="Failed to fetch ephemeral key"):
            encrypt_secret_bindings(
                mock_session,
                "ws",
                [SecretBinding(uri="x", value="y")],
            )


class TestResolveRegistryRef:
    """Tests for the registry-install REF resolver."""

    @staticmethod
    def _decls() -> RegistryDeclarations:
        return RegistryDeclarations(
            variables=[
                VariableDeclaration(
                    uri="https://example.com/crm#BatchSize",
                    variable_type="xsd:integer",
                    label="Batch size",
                    description=None,
                    required=True,
                ),
                VariableDeclaration(
                    uri="https://example.com/other/Region",
                    variable_type=None,
                    label=None,
                    description=None,
                    required=False,
                ),
            ],
            secrets=[
                SecretDeclaration(
                    uri="https://example.com/crm#ApiKey",
                    label=None,
                    description=None,
                    required=True,
                ),
            ],
        )

    def test_full_uri_passes_through_when_declared(self) -> None:
        decls = self._decls()
        assert (
            resolve_registry_ref("https://example.com/crm#BatchSize", decls)
            == "https://example.com/crm#BatchSize"
        )

    def test_full_uri_passes_through_even_when_undeclared(self) -> None:
        # Resolver returns URI as-is; declared-membership check happens
        # downstream in collect_bindings against variable_uris/secret_uris.
        decls = self._decls()
        assert (
            resolve_registry_ref("https://example.com/unknown#Foo", decls)
            == "https://example.com/unknown#Foo"
        )

    def test_unique_localname_resolves(self) -> None:
        decls = self._decls()
        assert (
            resolve_registry_ref("BatchSize", decls)
            == "https://example.com/crm#BatchSize"
        )
        assert (
            resolve_registry_ref("Region", decls) == "https://example.com/other/Region"
        )
        assert resolve_registry_ref("ApiKey", decls) == "https://example.com/crm#ApiKey"

    def test_ambiguous_localname_rejected(self) -> None:
        decls = RegistryDeclarations(
            variables=[
                VariableDeclaration(
                    uri="https://a.example/ns#Foo",
                    variable_type=None,
                    label=None,
                    description=None,
                    required=True,
                ),
                VariableDeclaration(
                    uri="https://b.example/ns#Foo",
                    variable_type=None,
                    label=None,
                    description=None,
                    required=True,
                ),
            ]
        )
        with pytest.raises(ValidationError, match="ambiguous"):
            resolve_registry_ref("Foo", decls)

    def test_unmatched_localname_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            resolve_registry_ref("DoesNotExist", self._decls())

    def test_qname_form_rejected(self) -> None:
        with pytest.raises(ValidationError, match="QName"):
            resolve_registry_ref("crm:BatchSize", self._decls())


class TestCollectBindingsRegistryPath:
    """``collect_bindings`` with a RegistryDeclarations input."""

    @staticmethod
    def _decls() -> RegistryDeclarations:
        return RegistryDeclarations(
            variables=[
                VariableDeclaration(
                    uri="https://example.com/crm#BatchSize",
                    variable_type="xsd:integer",
                    label="Batch size",
                    description=None,
                    required=True,
                )
            ],
            secrets=[
                SecretDeclaration(
                    uri="https://example.com/crm#ApiKey",
                    label="API Key",
                    description=None,
                    required=True,
                )
            ],
        )

    def test_localname_var_flag_resolves_against_registry_decls(self) -> None:
        vars_, secrets_ = collect_bindings(
            self._decls(),
            ["BatchSize=42"],
            ["ApiKey=TEST_KEY"],
            is_tty=False,
            env={"TEST_KEY": "shhh"},
        )
        assert vars_ == [
            VariableBinding(uri="https://example.com/crm#BatchSize", value="42")
        ]
        assert secrets_ == [
            SecretBinding(uri="https://example.com/crm#ApiKey", value="shhh")
        ]

    def test_undeclared_uri_raises(self) -> None:
        with pytest.raises(ValidationError, match="not declared"):
            collect_bindings(
                self._decls(),
                ["https://example.com/unknown#X=1"],
                [],
                is_tty=False,
            )

    def test_missing_required_in_non_tty_skipped_and_returned_empty(self) -> None:
        """Missing required bindings no longer raise — the platform accepts
        an install with no bindings and surfaces a WARNING event for any
        required declaration left unset. The CLI mirrors that contract.
        """
        vars_, secrets_ = collect_bindings(
            self._decls(),
            [],
            [],
            is_tty=False,
        )
        assert vars_ == []
        assert secrets_ == []
