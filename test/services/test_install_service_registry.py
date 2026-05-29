"""Tests for the registry-install path of ``plgt install``.

Covers ``execute_registry_install_workflow`` end-to-end: version resolution,
declaration fetch, binding validation, secret encryption hand-off, conflict
handling, and mapping to ``InstallResult``.
"""

from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest
from plgt.core.exceptions import ConflictError, ValidationError
from plgt.models.install import RegistryInstallConfig
from plgt.services.bindings import (
    SecretDeclaration,
    VariableDeclaration,
)
from plgt.services.install_service import execute_registry_install_workflow


class _StubProgress:
    """Minimal stand-in for ``rich.progress.Progress`` used by the workflow."""

    def add_task(self, *_a, **_k):
        return 0

    def update(self, *_a, **_k):
        return None

    def remove_task(self, *_a, **_k):
        return None

    def stop(self) -> None:
        # The workflow stops/starts the live region around interactive
        # prompts; both no-ops here since this stub doesn't render.
        return None

    def start(self) -> None:
        return None


@contextmanager
def _patched_workflow_dependencies(
    *,
    declared_variables=None,
    declared_secrets=None,
    versions=("2.1.0",),
    monitor_status="COMPLETED",
    install_response=None,
):
    """Patch every collaborator the registry-install workflow touches."""
    declared_variables = declared_variables or []
    declared_secrets = declared_secrets or []

    session = Mock()
    session.authenticated = True

    config_module = Mock()
    config_module.get_session.return_value = session
    config_module.defaults.get.return_value = "ws-1"

    registry_client = Mock()
    registry_client.get_versions.return_value = list(versions)
    registry_client.get_declarations.return_value = (
        declared_variables,
        declared_secrets,
    )

    lifecycle_client = Mock()
    lifecycle_client.install_from_registry.return_value = (
        install_response
        if install_response is not None
        else Mock(command_id="cmd-1", version="2.1.0")
    )

    with (
        patch("plgt.services.install_service.config", config_module),
        patch(
            "plgt.services.install_service.RegistryClient",
            return_value=registry_client,
        ),
        patch(
            "plgt.services.install_service.LifecycleCommandClient",
            return_value=lifecycle_client,
        ),
        patch(
            "plgt.services.install_service.monitor_command_events",
            return_value=monitor_status,
        ),
        patch(
            "plgt.services.install_service.encrypt_secret_bindings",
        ) as mock_encrypt,
    ):
        mock_encrypt.return_value = []
        yield {
            "session": session,
            "registry_client": registry_client,
            "lifecycle_client": lifecycle_client,
            "encrypt": mock_encrypt,
        }


class TestExecuteRegistryInstallWorkflow:
    def test_no_declarations_no_flags_skips_binding_calls(self) -> None:
        with _patched_workflow_dependencies() as stubs:
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
            )
            result = execute_registry_install_workflow(_StubProgress(), cfg)

            assert result.success is True
            assert result.command_id == "cmd-1"
            stubs["registry_client"].get_versions.assert_not_called()
            stubs["registry_client"].get_declarations.assert_called_once_with(
                "poliglot", "os", "2.1.0"
            )
            stubs["lifecycle_client"].install_from_registry.assert_called_once()
            kwargs = stubs["lifecycle_client"].install_from_registry.call_args[1]
            # Passes None when no version was provided would resolve client-
            # side, but here version is supplied — server still gets None for
            # bindings but version stays as supplied.
            assert kwargs["version"] == "2.1.0"
            assert kwargs["variable_bindings"] is None
            assert kwargs["secret_bindings"] is None

    def test_resolves_latest_when_version_omitted(self) -> None:
        with _patched_workflow_dependencies(
            versions=("2.2.0", "2.1.0", "2.0.0")
        ) as stubs:
            cfg = RegistryInstallConfig(
                publisher="poliglot", name="os", workspace="ws-1"
            )
            execute_registry_install_workflow(_StubProgress(), cfg)

            stubs["registry_client"].get_versions.assert_called_once_with(
                "poliglot", "os"
            )
            # Declarations fetched against the newest published version.
            stubs["registry_client"].get_declarations.assert_called_once_with(
                "poliglot", "os", "2.2.0"
            )
            # The CLI pins the install to the same version it validated
            # declarations against. Without this pin the platform's resolver
            # could pick a different version (publishedAt-desc vs semver,
            # workspace engine compatibility), which would silently mismatch
            # bindings.
            kwargs = stubs["lifecycle_client"].install_from_registry.call_args[1]
            assert kwargs["version"] == "2.2.0"

    def test_no_published_versions_raises(self) -> None:
        with _patched_workflow_dependencies(versions=()):
            cfg = RegistryInstallConfig(
                publisher="poliglot", name="ghost", workspace="ws-1"
            )
            with pytest.raises(ValidationError, match="No published versions"):
                execute_registry_install_workflow(_StubProgress(), cfg)

    def test_var_flag_resolved_against_registry_declarations(self) -> None:
        decls_vars = [
            VariableDeclaration(
                uri="https://example.com/crm#BatchSize",
                variable_type="xsd:integer",
                label="Batch size",
                description=None,
                required=True,
            )
        ]
        with _patched_workflow_dependencies(declared_variables=decls_vars) as stubs:
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
                var_flags=("BatchSize=42",),
            )
            execute_registry_install_workflow(_StubProgress(), cfg)

            kwargs = stubs["lifecycle_client"].install_from_registry.call_args[1]
            assert kwargs["variable_bindings"] == [
                {
                    "uri": "https://example.com/crm#BatchSize",
                    "value": "42",
                    "sourceMatrix": None,
                }
            ]

    def test_secret_flag_encrypted_before_install(self) -> None:
        decls_secrets = [
            SecretDeclaration(
                uri="https://example.com/crm#ApiKey",
                label="API Key",
                description=None,
                required=True,
            )
        ]
        with _patched_workflow_dependencies(declared_secrets=decls_secrets) as stubs:
            stubs["encrypt"].return_value = [
                Mock(
                    uri="https://example.com/crm#ApiKey",
                    key_id="key-1",
                    client_public_key="pk",
                    encrypted_value="ev",
                    nonce="n",
                )
            ]
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
                secret_from_env_flags=("ApiKey=MY_KEY",),
            )
            with patch.dict("os.environ", {"MY_KEY": "shhh"}, clear=False):
                execute_registry_install_workflow(_StubProgress(), cfg)

            stubs["encrypt"].assert_called_once()
            kwargs = stubs["lifecycle_client"].install_from_registry.call_args[1]
            assert kwargs["secret_bindings"] == [
                {
                    "uri": "https://example.com/crm#ApiKey",
                    "keyId": "key-1",
                    "clientPublicKey": "pk",
                    "encryptedValue": "ev",
                    "nonce": "n",
                }
            ]

    def test_flags_without_declarations_raises(self) -> None:
        with _patched_workflow_dependencies():
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
                var_flags=("BatchSize=42",),
            )
            with pytest.raises(ValidationError, match="declares no"):
                execute_registry_install_workflow(_StubProgress(), cfg)

    def test_failed_monitor_status_returns_non_success(self) -> None:
        with _patched_workflow_dependencies(monitor_status="FAILED"):
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
            )
            result = execute_registry_install_workflow(_StubProgress(), cfg)
            assert result.success is False
            assert result.status == "FAILED"


class TestRegistryInstallConflictHandling:
    """Parity with the local-build install path on 409 attach behavior."""

    @staticmethod
    def _conflict(existing_command_id: str = "cmd-existing") -> ConflictError:
        return ConflictError(
            "Active operation in progress",
            body={
                "existing": {
                    "commandId": existing_command_id,
                    "version": "2.1.0",
                },
                "requested": {"version": "2.1.0"},
            },
        )

    def test_409_silently_attaches_to_existing_command(self) -> None:
        with _patched_workflow_dependencies() as stubs:
            stubs[
                "lifecycle_client"
            ].install_from_registry.side_effect = self._conflict("cmd-existing")
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
            )
            result = execute_registry_install_workflow(_StubProgress(), cfg)
            # Attached to the in-flight command rather than failing.
            assert result.command_id == "cmd-existing"
            assert result.success is True

    def test_409_with_no_attach_raises_validation_error(self) -> None:
        with _patched_workflow_dependencies() as stubs:
            stubs[
                "lifecycle_client"
            ].install_from_registry.side_effect = self._conflict("cmd-existing")
            cfg = RegistryInstallConfig(
                publisher="poliglot",
                name="os",
                version="2.1.0",
                workspace="ws-1",
                no_attach=True,
            )
            with pytest.raises(ValidationError, match="--no-attach"):
                execute_registry_install_workflow(_StubProgress(), cfg)
