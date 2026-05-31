"""Unit tests for lifecycle commands.

Tests cover install, the `lifecycle list-commands` and
`lifecycle get-validation-report` group commands, and uninstall.
"""

import textwrap
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from plgt.cmd.lifecycle import app, lifecycle_app
from plgt.core.exceptions import ConflictError, ResourceNotFoundError, ValidationError
from plgt.models.lifecycle_command import (
    LifecycleCommand,
    LifecycleCommandResponse,
    LifecycleCommandStatus,
)
from typer.testing import CliRunner

runner = CliRunner()


DEMO_TTL = textwrap.dedent(
    """
    @prefix rdf:        <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs:       <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd:        <http://www.w3.org/2001/XMLSchema#> .
    @prefix plgt-mtx:   <https://poliglot.io/os/spec/matrix#> .
    @prefix plgt-build: <https://poliglot.io/os/spec/build#> .
    @prefix plgt-scrt:  <https://poliglot.io/os/spec/secrets#> .
    @prefix widget:     <https://example.com/widget#> .

    widget: a plgt-mtx:Matrix ;
         plgt-mtx:name "crm" .

    widget:SyncBatchSize a plgt-build:Variable ;
         plgt-build:variableType xsd:integer ;
         plgt-build:label "Sync Batch Size" ;
         plgt-build:required "true"^^xsd:boolean ;
         rdfs:isDefinedBy widget: .

    widget:WidgetServiceApiKey a plgt-scrt:ManagedSecret ;
         plgt-scrt:label "Widget Service API Key" ;
         plgt-scrt:required "true"^^xsd:boolean ;
         rdfs:isDefinedBy widget: .
    """
)


def _scaffold_demo_project(tmp_path: Path) -> Path:
    """Build a tiny on-disk project the install command can chew on."""
    project_dir = tmp_path / "crm-project"
    matrix_dir = project_dir / "crm"
    spec_dir = matrix_dir / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "crm.ttl").write_text(DEMO_TTL)
    (project_dir / "poliglot.yml").write_text(
        textwrap.dedent(
            """
            package:
              name: crm
              version: 0.0.1
              engineVersion: ">=1 <2"
            matrix:
              widget:
                path: ./crm
                spec:
                  - ./spec
            """
        ).lstrip()
    )
    return project_dir


class TestInstall:
    """Tests for the `install` command — flag parsing, declaration discovery,
    TTY/non-TTY prompts, 409 attach behavior, and --no-attach flag."""

    @patch("plgt.services.install_service.monitor_command_events")
    @patch("plgt.services.install_service.LifecycleCommandClient")
    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_with_flag_bindings_succeeds(
        self,
        mock_cmd_config,
        mock_svc_config,
        mock_client_class,
        mock_monitor,
        tmp_path,
    ):
        """--var + --secret-from-env flags satisfy required bindings without prompting."""
        project_dir = _scaffold_demo_project(tmp_path)

        # Workspace + auth
        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        # Pubkey response for secret encryption
        import base64 as _b64

        from nacl.public import PrivateKey

        server_priv = PrivateKey.generate()
        server_pub_b64 = _b64.b64encode(bytes(server_priv.public_key)).decode("ascii")

        def post_pubkey(*_args, **_kwargs):
            response = Mock()
            response.json.return_value = {
                "data": {"serverPublicKey": server_pub_b64, "keyId": "k-1"}
            }
            return response

        mock_session.post.side_effect = post_pubkey

        # Install client returns a successful response.
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.install_package.return_value = LifecycleCommandResponse(
            command_id="dep-123",
            package_name="crm",
            version="0.0.1",
            status="PENDING",
        )
        mock_monitor.return_value = "COMPLETED"

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
                "--var",
                "widget:SyncBatchSize=100",
                "--secret-from-env",
                "widget:WidgetServiceApiKey=WIDGET_KEY",
            ],
            env={"WIDGET_KEY": "supersecret"},
        )

        assert result.exit_code == 0, result.stdout
        # Bindings were forwarded into the install request.
        kwargs = mock_client.install_package.call_args[1]
        assert kwargs["variable_bindings"] == [
            {
                "uri": "https://example.com/widget#SyncBatchSize",
                "value": "100",
                "sourceMatrix": None,
            }
        ]
        assert len(kwargs["secret_bindings"]) == 1
        assert kwargs["secret_bindings"][0]["uri"] == (
            "https://example.com/widget#WidgetServiceApiKey"
        )
        assert kwargs["secret_bindings"][0]["keyId"] == "k-1"

    @patch("plgt.services.install_service.monitor_command_events")
    @patch("plgt.services.install_service.LifecycleCommandClient")
    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_non_tty_missing_required_proceeds_with_warning(
        self,
        mock_cmd_config,
        mock_svc_config,
        mock_client_class,
        mock_monitor,
        tmp_path,
    ):
        """Non-TTY install with no flags proceeds (no bindings, warning printed).

        Bindings are optional at install time — the platform accepts an
        install with no bindings and surfaces a warning event for any
        required declaration left unset. The CLI must NOT exit 1.
        """
        project_dir = _scaffold_demo_project(tmp_path)

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.install_package.return_value = LifecycleCommandResponse(
            command_id="dep-123",
            package_name="crm",
            version="0.0.1",
            status="PENDING",
        )
        mock_monitor.return_value = "COMPLETED"

        # CliRunner pipes stdin so isatty is False by default.
        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
            ],
        )

        assert result.exit_code == 0, result.stdout
        # Install proceeded with empty bindings — the platform handles the warning event.
        kwargs = mock_client.install_package.call_args[1]
        assert kwargs["variable_bindings"] == []
        assert kwargs["secret_bindings"] == []

    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_undeclared_qname_prefix_exits_one(
        self, mock_cmd_config, mock_svc_config, tmp_path
    ):
        project_dir = _scaffold_demo_project(tmp_path)

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
                "--var",
                "unknown:Foo=value",
            ],
        )

        assert result.exit_code == 1
        assert "prefix 'unknown' is not declared" in result.stdout

    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_secret_from_env_unset_exits_one(
        self, mock_cmd_config, mock_svc_config, tmp_path
    ):
        project_dir = _scaffold_demo_project(tmp_path)

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
                "--var",
                "widget:SyncBatchSize=100",
                "--secret-from-env",
                "widget:WidgetServiceApiKey=NEVER_SET_FOR_TEST",
            ],
            # Explicitly do NOT pass NEVER_SET_FOR_TEST in env.
            env={"NEVER_SET_FOR_TEST": ""},
        )

        # Note: env={"NEVER_SET_FOR_TEST": ""} sets it to empty, but we want
        # it unset. Confirm the test's environment really is missing the var.
        assert result.exit_code == 1
        # "is not set" wording from bindings.collect_bindings.
        assert "is not set" in result.stdout

    @patch("plgt.services.install_service.monitor_command_events")
    @patch("plgt.services.install_service.LifecycleCommandClient")
    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_409_same_version_silent_attach(
        self,
        mock_cmd_config,
        mock_svc_config,
        mock_client_class,
        mock_monitor,
        tmp_path,
    ):
        """409 with matching version → silent attach and continue monitoring."""
        # Project with no required declarations to keep the test focused on
        # 409 handling.
        project_dir = tmp_path / "noop"
        spec_dir = project_dir / "noop" / "spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "noop.ttl").write_text(
            textwrap.dedent(
                """
                @prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .
                @prefix noop: <https://example.com/noop#> .
                noop: a plgt-mtx:Matrix ; plgt-mtx:name "noop" .
                """
            )
        )
        (project_dir / "poliglot.yml").write_text(
            textwrap.dedent(
                """
                package:
                  name: noop
                  version: 0.0.1
                  engineVersion: ">=1 <2"
                matrix:
                  noop:
                    path: ./noop
                    spec: [./spec]
                """
            ).lstrip()
        )

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.install_package.side_effect = ConflictError(
            "active command exists",
            body={
                "conflict": "active_command",
                "existing": {
                    "commandId": "abc-123",
                    "packageName": "noop",
                    "version": "0.0.1",
                    "commandType": "INSTALL",
                    "status": "IN_PROGRESS",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
                "requested": {"version": "0.0.1"},
            },
        )
        mock_monitor.return_value = "COMPLETED"

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "Install already in progress" in result.stdout
        assert "abc-123" in result.stdout
        # Monitoring resumed against the existing commandId.
        monitored_command_id = mock_monitor.call_args[0][2]
        assert monitored_command_id == "abc-123"

    @patch("plgt.services.install_service.monitor_command_events")
    @patch("plgt.services.install_service.LifecycleCommandClient")
    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_409_version_mismatch_warns_and_attaches(
        self,
        mock_cmd_config,
        mock_svc_config,
        mock_client_class,
        mock_monitor,
        tmp_path,
    ):
        project_dir = tmp_path / "noop"
        spec_dir = project_dir / "noop" / "spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "noop.ttl").write_text(
            textwrap.dedent(
                """
                @prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .
                @prefix noop: <https://example.com/noop#> .
                noop: a plgt-mtx:Matrix ; plgt-mtx:name "noop" .
                """
            )
        )
        (project_dir / "poliglot.yml").write_text(
            textwrap.dedent(
                """
                package:
                  name: noop
                  version: 2.0.0
                  engineVersion: ">=1 <2"
                matrix:
                  noop:
                    path: ./noop
                    spec: [./spec]
                """
            ).lstrip()
        )

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.install_package.side_effect = ConflictError(
            "active command exists",
            body={
                "conflict": "active_command",
                "existing": {
                    "commandId": "abc-123",
                    "packageName": "noop",
                    "version": "1.0.0",
                    "commandType": "INSTALL",
                    "status": "IN_PROGRESS",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
                "requested": {"version": "2.0.0"},
            },
        )
        mock_monitor.return_value = "COMPLETED"

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "WARNING" in result.stdout
        assert "v1.0.0" in result.stdout
        # Monitoring still runs against the existing command.
        assert mock_monitor.call_args[0][2] == "abc-123"

    @patch("plgt.services.install_service.monitor_command_events")
    @patch("plgt.services.install_service.LifecycleCommandClient")
    @patch("plgt.services.install_service.config")
    @patch("plgt.cmd.lifecycle.config")
    def test_install_409_no_attach_exits_one(
        self,
        mock_cmd_config,
        mock_svc_config,
        mock_client_class,
        mock_monitor,
        tmp_path,
    ):
        project_dir = tmp_path / "noop"
        spec_dir = project_dir / "noop" / "spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "noop.ttl").write_text(
            textwrap.dedent(
                """
                @prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .
                @prefix noop: <https://example.com/noop#> .
                noop: a plgt-mtx:Matrix ; plgt-mtx:name "noop" .
                """
            )
        )
        (project_dir / "poliglot.yml").write_text(
            textwrap.dedent(
                """
                package:
                  name: noop
                  version: 0.0.1
                  engineVersion: ">=1 <2"
                matrix:
                  noop:
                    path: ./noop
                    spec: [./spec]
                """
            ).lstrip()
        )

        mock_session = Mock()
        mock_session.authenticated = True
        for cfg in (mock_cmd_config, mock_svc_config):
            cfg.get_session.return_value = mock_session
            cfg.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.install_package.side_effect = ConflictError(
            "active command exists",
            body={
                "conflict": "active_command",
                "existing": {
                    "commandId": "abc-123",
                    "packageName": "noop",
                    "version": "0.0.1",
                    "commandType": "INSTALL",
                    "status": "IN_PROGRESS",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
                "requested": {"version": "0.0.1"},
            },
        )

        result = runner.invoke(
            app,
            [
                "install",
                "--config",
                str(project_dir / "poliglot.yml"),
                "--workspace",
                "test-workspace",
                "--no-attach",
            ],
        )

        assert result.exit_code == 1
        assert "abc-123" in result.stdout
        # Did NOT attach — monitor not called.
        mock_monitor.assert_not_called()


class TestListLifecycleCommands:
    """Test the `lifecycle list-commands` command."""

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_success(self, mock_config, mock_client_class):
        """Test successfully listing commands."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = [
            LifecycleCommand(
                id="dep-1",
                package_installation_id="pkg-inst-1",
                package_name="test-package",
                version="1.0.0",
                status=LifecycleCommandStatus.COMPLETED,
                created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            ),
            LifecycleCommand(
                id="dep-2",
                package_installation_id="pkg-inst-1",
                package_name="test-package",
                version="1.0.1",
                status=LifecycleCommandStatus.PENDING,
                created_at=datetime(2025, 1, 2, 0, 0, 0, tzinfo=UTC),
                updated_at=datetime(2025, 1, 2, 0, 0, 0, tzinfo=UTC),
            ),
        ]

        result = runner.invoke(lifecycle_app, ["list-commands", "test-package"])

        assert result.exit_code == 0
        assert "dep-1" in result.stdout
        assert "dep-2" in result.stdout
        assert "1.0.0" in result.stdout
        assert "COMPLETED" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_with_workspace(self, mock_config, mock_client_class):
        """Test listing commands with explicit workspace."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = []

        result = runner.invoke(
            lifecycle_app,
            ["list-commands", "test-package", "--workspace", "my-workspace"],
        )

        assert result.exit_code == 0
        mock_client.list_commands.assert_called_once_with(
            "my-workspace", "test-package", size=10
        )

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_with_limit(self, mock_config, mock_client_class):
        """Test listing commands with custom limit."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = []

        result = runner.invoke(
            lifecycle_app,
            ["list-commands", "test-package", "--limit", "5"],
        )

        assert result.exit_code == 0
        mock_client.list_commands.assert_called_once_with(
            "test-workspace", "test-package", size=5
        )

    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_no_workspace(self, mock_config):
        """Test error when no workspace configured."""
        mock_config.defaults.get.return_value = None

        result = runner.invoke(lifecycle_app, ["list-commands", "test-package"])

        assert result.exit_code == 1
        assert "No workspace" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_not_authenticated(self, mock_config):
        """Test error when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(lifecycle_app, ["list-commands", "test-package"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_empty(self, mock_config, mock_client_class):
        """Test handling empty command list."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = []

        result = runner.invoke(lifecycle_app, ["list-commands", "test-package"])

        assert result.exit_code == 0
        assert "No commands found" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_list_commands_shows_error_message(self, mock_config, mock_client_class):
        """Test that failed commands show error messages."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = [
            LifecycleCommand(
                id="dep-fail",
                package_installation_id="pkg-inst-1",
                package_name="test-package",
                version="1.0.0",
                status=LifecycleCommandStatus.FAILED,
                error_message="Validation failed: missing required property",
                created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            )
        ]

        result = runner.invoke(lifecycle_app, ["list-commands", "test-package"])

        assert result.exit_code == 0
        assert "FAILED" in result.stdout
        assert "Validation failed" in result.stdout


class TestGetLifecycleValidationReport:
    """Test the `lifecycle get-validation-report` command."""

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_by_id(self, mock_config, mock_client_class):
        """Test getting validation report by command ID."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        ttl_content = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
[] a sh:ValidationReport ;
   sh:conforms true .
"""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_validation_report.return_value = ttl_content

        result = runner.invoke(lifecycle_app, ["get-validation-report", "dep-123"])

        assert result.exit_code == 0
        assert "sh:ValidationReport" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_latest(self, mock_config, mock_client_class):
        """Test getting validation report for latest command."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = [
            LifecycleCommand(
                id="dep-latest",
                package_installation_id="pkg-inst-1",
                package_name="test-package",
                version="1.0.0",
                status=LifecycleCommandStatus.COMPLETED,
                created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            )
        ]
        mock_client.get_validation_report.return_value = "@prefix sh: <...> ."

        result = runner.invoke(
            lifecycle_app,
            [
                "get-validation-report",
                "--latest",
                "--package",
                "test-package",
            ],
        )

        assert result.exit_code == 0
        mock_client.list_commands.assert_called_once()
        mock_client.get_validation_report.assert_called_once_with(
            "test-workspace", "dep-latest"
        )

    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_latest_without_package(self, mock_config):
        """Test error when using --latest without --package."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(lifecycle_app, ["get-validation-report", "--latest"])

        assert result.exit_code == 1
        assert "--package is required" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_no_id_no_latest(self, mock_config):
        """Test error when neither command ID nor --latest provided."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(lifecycle_app, ["get-validation-report"])

        assert result.exit_code == 1
        assert "command ID" in result.stdout or "--latest" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_no_workspace(self, mock_config):
        """Test error when no workspace configured."""
        mock_config.defaults.get.return_value = None

        result = runner.invoke(lifecycle_app, ["get-validation-report", "dep-123"])

        assert result.exit_code == 1
        assert "No workspace" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_not_authenticated(self, mock_config):
        """Test error when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(lifecycle_app, ["get-validation-report", "dep-123"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_not_found(self, mock_config, mock_client_class):
        """Test handling when no validation report exists."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_validation_report.return_value = None

        result = runner.invoke(lifecycle_app, ["get-validation-report", "dep-123"])

        assert result.exit_code == 0
        assert "No validation report found" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_latest_no_commands(
        self, mock_config, mock_client_class
    ):
        """Test error when using --latest but no commands exist."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.list_commands.return_value = []

        result = runner.invoke(
            lifecycle_app,
            [
                "get-validation-report",
                "--latest",
                "--package",
                "test-package",
            ],
        )

        assert result.exit_code == 0
        assert "No commands found" in result.stdout

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_with_workspace(self, mock_config, mock_client_class):
        """Test getting validation report with explicit workspace."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_validation_report.return_value = "@prefix sh: <...> ."

        result = runner.invoke(
            lifecycle_app,
            [
                "get-validation-report",
                "dep-123",
                "--workspace",
                "my-workspace",
            ],
        )

        assert result.exit_code == 0
        mock_client.get_validation_report.assert_called_once_with(
            "my-workspace", "dep-123"
        )

    @patch("plgt.cmd.lifecycle.display_validation_report")
    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_get_validation_report_formatted(
        self, mock_config, mock_client_class, mock_display
    ):
        """Test getting formatted validation report."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        ttl_content = "@prefix sh: <...> ."
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.get_validation_report.return_value = ttl_content

        result = runner.invoke(
            lifecycle_app,
            ["get-validation-report", "dep-123", "--formatted"],
        )

        assert result.exit_code == 0
        # Verify display_validation_report was called
        mock_display.assert_called_once()


class TestUninstall:
    """Test uninstall command."""

    @patch("plgt.cmd.lifecycle.monitor_command_events")
    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_success_with_yes(
        self, mock_config, mock_client_class, mock_monitor
    ):
        """Test successful uninstall with --yes flag (skip confirmation)."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.uninstall_package.return_value = LifecycleCommandResponse(
            command_id="cmd-123",
            package_name="test-package",
            version="1.0.0",
            status="PENDING",
        )
        mock_monitor.return_value = "COMPLETED"

        result = runner.invoke(app, ["uninstall", "test-package", "--yes"])

        assert result.exit_code == 0
        assert "uninstalled" in result.stdout.lower()
        mock_client.uninstall_package.assert_called_once_with(
            "test-workspace", "test-package"
        )
        mock_monitor.assert_called_once()

    @patch("plgt.cmd.lifecycle.monitor_command_events")
    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_with_confirmation(
        self, mock_config, mock_client_class, mock_monitor
    ):
        """Test uninstall with confirmation prompt (user confirms)."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.uninstall_package.return_value = LifecycleCommandResponse(
            command_id="cmd-123",
            package_name="test-package",
            version="1.0.0",
            status="PENDING",
        )
        mock_monitor.return_value = "COMPLETED"

        result = runner.invoke(app, ["uninstall", "test-package"], input="y\n")

        assert result.exit_code == 0
        mock_client.uninstall_package.assert_called_once()

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_cancelled(self, mock_config, mock_client_class):
        """Test uninstall cancelled by user at confirmation prompt."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(app, ["uninstall", "test-package"], input="n\n")

        assert result.exit_code == 0
        assert "cancelled" in result.stdout.lower()
        mock_client_class.return_value.uninstall_package.assert_not_called()

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_not_found(self, mock_config, mock_client_class):
        """Test uninstall when package is not installed."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.uninstall_package.side_effect = ResourceNotFoundError(
            "Package not found"
        )

        result = runner.invoke(app, ["uninstall", "test-package", "--yes"])

        assert result.exit_code == 1
        assert "not installed" in result.stdout.lower()

    @patch("plgt.cmd.lifecycle.LifecycleCommandClient")
    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_conflict(self, mock_config, mock_client_class):
        """Test uninstall when package has active operation."""
        mock_session = Mock()
        mock_session.authenticated = True
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.uninstall_package.side_effect = ValidationError(
            "Package has an active INSTALL operation"
        )

        result = runner.invoke(app, ["uninstall", "test-package", "--yes"])

        assert result.exit_code == 1
        assert "INSTALL" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_no_workspace(self, mock_config):
        """Test error when no workspace configured."""
        mock_config.defaults.get.return_value = None

        result = runner.invoke(app, ["uninstall", "test-package", "--yes"])

        assert result.exit_code == 1
        assert "No workspace" in result.stdout

    @patch("plgt.cmd.lifecycle.config")
    def test_uninstall_not_authenticated(self, mock_config):
        """Test error when not authenticated."""
        mock_session = Mock()
        mock_session.authenticated = False
        mock_config.get_session.return_value = mock_session
        mock_config.defaults.get.return_value = "test-workspace"

        result = runner.invoke(app, ["uninstall", "test-package", "--yes"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout


class TestInstallRegistryPath:
    """Test ``plgt install <publisher>/<name>[@<version>]`` (registry path)."""

    def _ok_install_result(self):
        # Match InstallResult NamedTuple shape returned by the workflow.
        from plgt.models.install import InstallResult

        return InstallResult(
            command_id="cmd-reg-1",
            status="COMPLETED",
            matrix_uri="poliglot/os",
            version="2.1.0",
            artifact_file=Path(),
            success=True,
            error_message=None,
        )

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_routes_registry_target_to_registry_workflow(self, mock_workflow):
        """Positional ref 'publisher/name@version' triggers the registry path."""
        mock_workflow.return_value = self._ok_install_result()

        result = runner.invoke(
            app, ["install", "poliglot/os@2.1.0", "--workspace", "ws-1"]
        )

        assert result.exit_code == 0, result.stdout
        assert mock_workflow.call_count == 1
        cfg = mock_workflow.call_args[0][1]
        assert cfg.publisher == "poliglot"
        assert cfg.name == "os"
        assert cfg.version == "2.1.0"
        assert cfg.workspace == "ws-1"

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_no_version_passes_none(self, mock_workflow):
        mock_workflow.return_value = self._ok_install_result()

        result = runner.invoke(app, ["install", "poliglot/os", "--workspace", "ws-1"])

        assert result.exit_code == 0, result.stdout
        cfg = mock_workflow.call_args[0][1]
        assert cfg.version is None

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_passes_auto_update_and_bindings(self, mock_workflow):
        mock_workflow.return_value = self._ok_install_result()

        result = runner.invoke(
            app,
            [
                "install",
                "poliglot/os@2.1.0",
                "--workspace",
                "ws-1",
                "--auto-update",
                "--var",
                "BatchSize=42",
                "--secret-from-env",
                "ApiKey=MY_KEY",
            ],
        )

        assert result.exit_code == 0, result.stdout
        cfg = mock_workflow.call_args[0][1]
        assert cfg.auto_update is True
        assert cfg.var_flags == ("BatchSize=42",)
        assert cfg.secret_from_env_flags == ("ApiKey=MY_KEY",)

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_failed_result_exits_one(self, mock_workflow):
        from plgt.models.install import InstallResult

        mock_workflow.return_value = InstallResult(
            command_id="cmd-reg-2",
            status="FAILED",
            matrix_uri="poliglot/os",
            version="2.1.0",
            artifact_file=Path(),
            success=False,
            error_message="boom",
        )

        result = runner.invoke(
            app, ["install", "poliglot/os@2.1.0", "--workspace", "ws-1"]
        )

        assert result.exit_code == 1

    def test_install_registry_invalid_ref_at_without_version(self):
        result = runner.invoke(app, ["install", "poliglot/os@", "--workspace", "ws-1"])
        assert result.exit_code == 1
        assert "no version" in result.stdout.lower()

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_at_latest_normalizes_to_none(self, mock_workflow):
        """`@latest` is sugar for "no version pin"; normalises to version=None."""
        mock_workflow.return_value = self._ok_install_result()

        result = runner.invoke(
            app, ["install", "poliglot/os@latest", "--workspace", "ws-1"]
        )

        assert result.exit_code == 0, result.stdout
        cfg = mock_workflow.call_args[0][1]
        assert cfg.version is None

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_passes_no_attach_through(self, mock_workflow):
        mock_workflow.return_value = self._ok_install_result()

        runner.invoke(
            app,
            [
                "install",
                "poliglot/os@2.1.0",
                "--workspace",
                "ws-1",
                "--no-attach",
            ],
        )

        cfg = mock_workflow.call_args[0][1]
        assert cfg.no_attach is True

    @patch("plgt.cmd.lifecycle.execute_registry_install_workflow")
    def test_install_registry_warns_on_local_only_flags(self, mock_workflow):
        mock_workflow.return_value = self._ok_install_result()

        result = runner.invoke(
            app,
            [
                "install",
                "poliglot/os@2.1.0",
                "--workspace",
                "ws-1",
                "--release-notes",
                "ignored",
                "--force",
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "release-notes is ignored" in result.stdout
        assert "--force is ignored" in result.stdout
        # Workflow ran despite the warnings.
        assert mock_workflow.call_count == 1

    def test_install_registry_rejects_invalid_publisher_slug(self):
        """A bad publisher slug routes to ``_parse_registry_ref`` (since the
        path-shape filter doesn't match) and gets the specific slug error."""
        result = runner.invoke(
            app, ["install", "Bad_Publisher/plgt", "--workspace", "ws-1"]
        )
        assert result.exit_code == 1
        assert "publisher" in result.stdout.lower()
        assert "lowercase alphanumeric" in result.stdout.lower()

    def test_install_registry_rejects_invalid_package_name(self):
        result = runner.invoke(
            app, ["install", "poliglot/Bad_Name", "--workspace", "ws-1"]
        )
        assert result.exit_code == 1
        assert "package name" in result.stdout.lower()
        assert "lowercase alphanumeric" in result.stdout.lower()

    @pytest.mark.parametrize(
        "path_shape",
        [
            "./poliglot.yml",
            "../foo",
            "/etc/passwd",
            "~/projects/foo",
            "https://example.com/foo",
        ],
    )
    def test_install_rejects_path_shaped_target(self, path_shape):
        """Filesystem paths and URLs that contain ``/`` must NOT be parsed
        as registry refs — they get a clear "looks like a path" error.
        """
        result = runner.invoke(app, ["install", path_shape, "--workspace", "ws-1"])
        assert result.exit_code == 1
        assert "looks like a filesystem path or URL" in result.stdout


class TestRangeFromVersionArg:
    """Cover the minor-floor range derivation policy that backs `plgt install
    <pub>/<name>[@<ver>]`.
    """

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("1.5.2", ">=1.5 <2"),
            ("1.5", ">=1.5 <2"),
            ("1", ">=1 <2"),
            ("0.3.1", ">=0.3 <1"),
            ("10.0.0", ">=10.0 <11"),
        ],
    )
    def test_minor_floor_derivation_from_concrete_versions(
        self, version: str, expected: str
    ) -> None:
        from plgt.cmd.lifecycle import _range_from_version_arg

        assert _range_from_version_arg(version) == expected

    def test_none_returns_permissive_sentinel(self) -> None:
        from plgt.cmd.lifecycle import _range_from_version_arg

        # The caller is expected to rewrite this sentinel to a real minor-floor range after
        # install resolves a concrete version. Persisting ">=0" verbatim is a bug.
        assert _range_from_version_arg(None) == ">=0"
        assert _range_from_version_arg("latest") == ">=0"

    def test_invalid_version_raises(self) -> None:
        from plgt.cmd.lifecycle import _range_from_version_arg

        with pytest.raises(ValidationError):
            _range_from_version_arg("not-a-version")


class TestLocalDepsInstallSentinelRewrite:
    """End-to-end coverage of `_run_local_deps_install`'s post-install yml
    rewrite. Two paths matter: (a) the resolver reported the freshly-installed
    dep and we rewrite ">=0" to the resolved minor-floor; (b) the resolver
    did NOT report it and we fail fast rather than persist the sentinel.
    """

    @staticmethod
    def _poliglot_yml(tmp_path: Path) -> Path:
        config = tmp_path / "poliglot.yml"
        config.write_text(
            textwrap.dedent(
                """
                version: "1"
                package:
                  name: "demo"
                  version: "0.1.0"
                  engineVersion: ">=1 <2"
                """
            ).strip()
            + "\n"
        )
        return config

    def test_resolved_version_rewrites_to_minor_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plgt.cmd.lifecycle import _run_local_deps_install
        from plgt.services.deps_install_service import InstallSummary
        from plgt.services.deps_lockfile import LockedPackage

        config_path = self._poliglot_yml(tmp_path)
        monkeypatch.chdir(tmp_path)

        engine_pkg = LockedPackage(
            publisher="poliglot",
            name="os",
            version="2.1.0",
            checksum="sha256:engine",
            root=True,
        )
        # The resolver picks 1.5.2; we expect yml to record `>=1.5 <2` (minor-floor).
        target_pkg = LockedPackage(
            publisher="widget",
            name="widget",
            version="1.5.2",
            checksum="sha256:target",
            root=True,
        )
        summary = InstallSummary(
            engine=engine_pkg,
            dependencies=[target_pkg],
            lockfile_path=tmp_path / ".matrix" / "deps.lock",
            fetched=[engine_pkg, target_pkg],
            cached=[],
        )

        monkeypatch.setattr(
            "plgt.cmd.lifecycle.install_local_deps", lambda *a, **kw: summary
        )
        monkeypatch.setattr("plgt.cmd.lifecycle.APISession", lambda: Mock())

        _run_local_deps_install(target="widget/widget", no_save=False, update=False)

        import yaml

        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        assert cfg["dependencies"]["widget/widget"] == ">=1.5 <2"

    def test_resolver_missing_entry_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from plgt.cmd.lifecycle import _run_local_deps_install
        from plgt.core.exceptions import ServiceError
        from plgt.services.deps_install_service import InstallSummary
        from plgt.services.deps_lockfile import LockedPackage

        config_path = self._poliglot_yml(tmp_path)
        monkeypatch.chdir(tmp_path)

        engine_pkg = LockedPackage(
            publisher="poliglot",
            name="os",
            version="2.1.0",
            checksum="sha256:engine",
            root=True,
        )
        # Resolver returns empty dependency list — our just-installed dep is absent.
        summary = InstallSummary(
            engine=engine_pkg,
            dependencies=[],
            lockfile_path=tmp_path / ".matrix" / "deps.lock",
            fetched=[engine_pkg],
            cached=[],
        )

        monkeypatch.setattr(
            "plgt.cmd.lifecycle.install_local_deps", lambda *a, **kw: summary
        )
        monkeypatch.setattr("plgt.cmd.lifecycle.APISession", lambda: Mock())

        with pytest.raises(ServiceError, match="Resolver did not report"):
            _run_local_deps_install(target="widget/widget", no_save=False, update=False)

        # yml must be untouched if the install raised.
        import yaml

        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        # No dependencies key got written.
        assert "dependencies" not in cfg or not cfg.get("dependencies")

    def test_explicit_version_skips_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the user passes `@1.5.2`, the early write already produced
        `>=1.5 <2`, so the post-install rewrite branch is skipped entirely.
        Same yml outcome, but via a different code path.
        """
        from plgt.cmd.lifecycle import _run_local_deps_install
        from plgt.services.deps_install_service import InstallSummary
        from plgt.services.deps_lockfile import LockedPackage

        config_path = self._poliglot_yml(tmp_path)
        monkeypatch.chdir(tmp_path)

        engine_pkg = LockedPackage(
            publisher="poliglot",
            name="os",
            version="2.1.0",
            checksum="sha256:engine",
            root=True,
        )
        target_pkg = LockedPackage(
            publisher="widget",
            name="widget",
            version="1.5.2",
            checksum="sha256:target",
            root=True,
        )
        # Note: the summary is deliberately empty for `dependencies` — we want to confirm the
        # rewrite branch never runs and the yml still gets the explicit minor-floor range.
        summary = InstallSummary(
            engine=engine_pkg,
            dependencies=[],
            lockfile_path=tmp_path / ".matrix" / "deps.lock",
            fetched=[engine_pkg, target_pkg],
            cached=[],
        )

        monkeypatch.setattr(
            "plgt.cmd.lifecycle.install_local_deps", lambda *a, **kw: summary
        )
        monkeypatch.setattr("plgt.cmd.lifecycle.APISession", lambda: Mock())

        _run_local_deps_install(
            target="widget/widget@1.5.2", no_save=False, update=False
        )

        import yaml

        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        assert cfg["dependencies"]["widget/widget"] == ">=1.5 <2"


class TestInstallNoLongerHandlesLocalDeps:
    """`plgt install` is now reserved for workspace pushes. Legacy bare /
    no-workspace invocations must point users at `plgt sync` / `plgt add`
    rather than silently running the old local-deps flow.
    """

    def test_bare_install_points_at_sync(self) -> None:
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 1
        assert "plgt sync" in result.stdout

    def test_install_with_target_no_workspace_points_at_add(self) -> None:
        result = runner.invoke(app, ["install", "poliglot/os"])
        assert result.exit_code == 1
        assert "plgt add" in result.stdout


class TestRemoveDependencyFromConfig:
    """Covers the new ``remove_dependency_from_config`` helper that backs
    ``plgt remove``.
    """

    def test_removes_existing_entry(self, tmp_path: Path) -> None:
        from plgt.services.deps_install_service import remove_dependency_from_config

        config_path = tmp_path / "poliglot.yml"
        config_path.write_text(
            'version: "1"\n'
            "package:\n"
            '  name: "demo"\n'
            "dependencies:\n"
            '  "widget/widget": ">=1 <2"\n'
            '  "acme/shared": ">=0.3 <1"\n'
        )
        removed = remove_dependency_from_config(
            config_path, publisher="widget", name="widget"
        )
        assert removed is True

        import yaml

        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        assert cfg["dependencies"] == {"acme/shared": ">=0.3 <1"}

    def test_returns_false_for_missing_entry(self, tmp_path: Path) -> None:
        from plgt.services.deps_install_service import remove_dependency_from_config

        config_path = tmp_path / "poliglot.yml"
        config_path.write_text(
            'version: "1"\n'
            "package:\n"
            '  name: "demo"\n'
            "dependencies:\n"
            '  "widget/widget": ">=1 <2"\n'
        )
        removed = remove_dependency_from_config(
            config_path, publisher="ghost", name="nope"
        )
        assert removed is False

    def test_empties_to_dropped_key(self, tmp_path: Path) -> None:
        """Removing the last entry drops the whole `dependencies:` key."""
        from plgt.services.deps_install_service import remove_dependency_from_config

        config_path = tmp_path / "poliglot.yml"
        config_path.write_text(
            'version: "1"\n'
            "package:\n"
            '  name: "demo"\n'
            "dependencies:\n"
            '  "widget/widget": ">=1 <2"\n'
        )
        remove_dependency_from_config(config_path, publisher="widget", name="widget")

        import yaml

        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        assert "dependencies" not in cfg


class TestAddCommand:
    """`plgt add <pub>/<name>` writes to poliglot.yml then syncs (unless
    --no-sync). With --no-sync the manifest mutation is the only effect.
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        config = tmp_path / "poliglot.yml"
        config.write_text(
            'version: "1"\n'
            "package:\n"
            '  name: "demo"\n'
            '  version: "0.1.0"\n'
            '  engineVersion: ">=1 <2"\n'
        )
        return config

    def test_add_no_sync_writes_yml_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._project(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["add", "widget/widget@1.5.2", "--no-sync"])
        assert result.exit_code == 0, result.stdout

        import yaml

        with (tmp_path / "poliglot.yml").open() as f:
            cfg = yaml.safe_load(f)
        assert cfg["dependencies"]["widget/widget"] == ">=1.5 <2"

    def test_add_invalid_ref_errors_early(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._project(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["add", "bare-name", "--no-sync"])
        assert result.exit_code == 1

    def test_add_no_sync_without_version_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``plgt add foo/bar --no-sync`` without ``@version`` would write
        the ``>=0`` sentinel permanently (Phase A regression). The command
        must refuse so the user pins explicitly or drops --no-sync.
        """
        self._project(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["add", "widget/widget", "--no-sync"])
        assert result.exit_code == 1
        assert "@version" in result.stdout

    def test_add_with_sync_rewrites_yml_to_minor_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locks in the Phase A contract: bare `plgt add foo/bar` (no
        @version) resolves through the resolver and rewrites the yml-bound
        range to the resolved version's minor floor. The ``>=0`` sentinel
        must NOT persist after a successful add-with-sync.
        """
        from plgt.services.deps_install_service import InstallSummary
        from plgt.services.deps_lockfile import LockedPackage

        self._project(tmp_path)
        monkeypatch.chdir(tmp_path)

        engine_pkg = LockedPackage(
            publisher="poliglot",
            name="os",
            version="2.1.0",
            checksum="sha256:engine",
            root=True,
        )
        resolved = LockedPackage(
            publisher="widget",
            name="widget",
            version="1.5.2",
            checksum="sha256:target",
            root=True,
        )
        summary = InstallSummary(
            engine=engine_pkg,
            dependencies=[resolved],
            lockfile_path=tmp_path / ".matrix" / "deps.lock",
            fetched=[engine_pkg, resolved],
            cached=[],
        )

        monkeypatch.setattr(
            "plgt.cmd.lifecycle.install_local_deps", lambda *a, **kw: summary
        )
        monkeypatch.setattr("plgt.cmd.lifecycle.APISession", lambda: Mock())

        result = runner.invoke(app, ["add", "widget/widget", "--from-registry"])
        assert result.exit_code == 0, result.stdout

        import yaml

        with (tmp_path / "poliglot.yml").open() as f:
            cfg = yaml.safe_load(f)
        assert cfg["dependencies"]["widget/widget"] == ">=1.5 <2"


class TestRemoveCommand:
    """`plgt remove <pub>/<name>` mirrors `plgt add`. --no-sync mutates the
    manifest only.
    """

    def test_remove_no_sync_drops_yml_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "poliglot.yml"
        config.write_text(
            'version: "1"\n'
            "package:\n"
            '  name: "demo"\n'
            '  engineVersion: ">=1 <2"\n'
            "dependencies:\n"
            '  "widget/widget": ">=1 <2"\n'
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["remove", "widget/widget", "--no-sync"])
        assert result.exit_code == 0, result.stdout

        import yaml

        with config.open() as f:
            cfg = yaml.safe_load(f)
        assert "dependencies" not in cfg

    def test_remove_missing_entry_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "poliglot.yml"
        config.write_text(
            'version: "1"\npackage:\n  name: "demo"\n  engineVersion: ">=1 <2"\n'
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["remove", "widget/widget", "--no-sync"])
        assert result.exit_code == 1
        assert "is not in poliglot.yml" in result.stdout
