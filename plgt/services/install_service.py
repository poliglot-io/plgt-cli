"""Install service for package installation workflow.

This module provides the installation workflow for building and installing
multi-matrix packages to the platform.
"""

import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from plgt.clients.lifecycle_command_client import LifecycleCommandClient
from plgt.clients.registry_client import RegistryClient
from plgt.core import config
from plgt.core.exceptions import (
    ConflictError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError,
)
from plgt.models.build_types import PackageBuildResult, PackageConfig
from plgt.models.install import InstallConfig, InstallResult, RegistryInstallConfig
from plgt.services.bindings import (
    RegistryDeclarations,
    collect_bindings,
    discover_project_declarations,
    encrypt_secret_bindings,
)
from plgt.services.command_monitor import monitor_command_events

logger = logging.getLogger(__name__)


def create_install_config(
    config_file: Path | None,
    workspace: str | None,
    release_notes: str | None,
    force: bool,
    *,
    var_flags: tuple[str, ...] = (),
    secret_from_env_flags: tuple[str, ...] = (),
    no_attach: bool = False,
) -> InstallConfig:
    """Create installation configuration.

    Args:
        config_file: Path to poliglot.yml configuration file.
        workspace: Workspace name (default: from config).
        release_notes: Optional release notes.
        force: Force update flag.
        var_flags: Raw ``--var`` flag values.
        secret_from_env_flags: Raw ``--secret-from-env`` flag values.
        no_attach: When True, exit on 409 active-command conflict instead of
            attaching to the in-flight install.

    Returns:
        InstallConfig with resolved values.
    """
    return InstallConfig(
        config_file=config_file,
        workspace=workspace,
        release_notes=release_notes,
        force=force,
        var_flags=var_flags,
        secret_from_env_flags=secret_from_env_flags,
        no_attach=no_attach,
    )


def execute_install_workflow(progress, install_config: InstallConfig) -> InstallResult:
    """Execute the complete package installation workflow.

    This builds all matrices in the package and installs the package.tgz
    to the platform for processing.

    Args:
        progress: Rich progress instance for status updates
        install_config: Installation configuration

    Returns:
        InstallResult with installation status
    """
    from plgt.services.build_service import build_package, create_package_config

    console = Console()

    # Resolve install-time bindings BEFORE the build — Rich Progress owns the
    # terminal while it's live, so any typer.prompt() inside a progress block
    # writes its prompt into the live region and the user sees nothing. Pull
    # the interactive step in front of the build (and surface QName/flag
    # errors as a fast fail before any work happens).
    package_config = create_package_config(install_config.config_file)
    progress.stop()
    try:
        variable_bindings, secret_bindings = _resolve_bindings(
            package_config, install_config, console
        )
    finally:
        progress.start()

    # Build the package
    build_result = build_package(progress, package_config)

    # Get workspace for display
    workspace = install_config.workspace or config.defaults.get("workspace", "unknown")
    matrix_names = ", ".join(m.name for m in build_result.matrices)

    # Display installation header (newline first to clear any spinner remnants)
    console.print()
    header_content = (
        f"[bold]Installing:[/bold] {build_result.package_name} v{build_result.package_version} → {workspace}\n"
        f"[bold]Matrices:[/bold]  {matrix_names}"
    )
    console.print(Panel(header_content, expand=False))
    console.print()

    # Install the package
    return _install_package(
        progress,
        install_config,
        build_result,
        console,
        variable_bindings,
        secret_bindings,
    )


def _resolve_bindings(
    package_config: PackageConfig,
    install_config: InstallConfig,
    console: Console,
) -> tuple[list[dict], list[dict]]:
    """Discover declarations and collect/encrypt bindings.

    Returns ``(variable_bindings_payload, secret_bindings_payload)`` with
    each item shaped for the install request body. Both lists are empty when
    the project declares no variables/secrets and no flags were supplied.
    """
    var_flags = list(install_config.var_flags)
    secret_env_flags = list(install_config.secret_from_env_flags)

    declarations = discover_project_declarations(package_config)

    if not declarations.variables and not declarations.secrets:
        if var_flags or secret_env_flags:
            msg = (
                "--var/--secret-from-env supplied but the project declares "
                "no plgt-build:Variable / plgt-scrt:ManagedSecret resources"
            )
            raise ValidationError(msg)
        return [], []

    plain_vars, plain_secrets = collect_bindings(
        declarations,
        var_flags,
        secret_env_flags,
        console=console,
    )

    # Encrypt secrets via the platform `/pubkey` flow before we upload.
    encrypted_secrets: list[dict] = []
    if plain_secrets:
        # Resolve workspace early — secret encryption is workspace-scoped.
        # The CLI gate (`plgt install` requires explicit --workspace) ensures
        # install_config.workspace is set by the time we get here; treat the absence as a
        # programmer error rather than re-falling back to a default workspace, which would
        # reintroduce the "push to the wrong workspace by accident" trap.
        workspace = install_config.workspace
        if not workspace:
            msg = (
                "install_service was invoked with no workspace. The CLI front door is "
                "supposed to enforce --workspace; this is a bug."
            )
            raise ValidationError(msg)
        api_session = config.get_session()
        if not getattr(api_session, "authenticated", False):
            msg = "Not authenticated. Run 'plgt auth login' first."
            raise ValidationError(msg)
        console.print(
            f"[dim]Encrypting {len(plain_secrets)} secret binding(s)...[/dim]"
        )
        encrypted = encrypt_secret_bindings(api_session, workspace, plain_secrets)
        encrypted_secrets.extend(
            {
                "uri": esb.uri,
                "keyId": esb.key_id,
                "clientPublicKey": esb.client_public_key,
                "encryptedValue": esb.encrypted_value,
                "nonce": esb.nonce,
            }
            for esb in encrypted
        )

    variable_bindings = [
        {
            "uri": b.uri,
            "value": b.value,
            "sourceMatrix": b.source_matrix,
        }
        for b in plain_vars
    ]
    return variable_bindings, encrypted_secrets


def _install_package(
    progress,
    install_config: InstallConfig,
    build_result: PackageBuildResult,
    console: Console,
    variable_bindings: list[dict],
    secret_bindings: list[dict],
) -> InstallResult:
    """Install a built package to the platform.

    Args:
        progress: Rich progress instance.
        install_config: Installation configuration.
        build_result: Result from package build.
        console: Console for output.
        variable_bindings: Resolved plaintext variable bindings (request shape).
        secret_bindings: E2E-encrypted secret bindings (request shape).

    Returns:
        InstallResult with installation status.
    """
    task = progress.add_task("Uploading package...", total=None)

    try:
        # The CLI front door (`plgt install --workspace ...`) enforces an explicit slug.
        # Treat absence as a bug rather than falling back to a default workspace, which
        # would silently push to the wrong target.
        workspace = install_config.workspace
        if not workspace:
            msg = (
                "install_service was invoked with no workspace. The CLI front door is "
                "supposed to enforce --workspace; this is a bug."
            )
            raise ValidationError(msg)

        # Get authenticated API session
        api_session = config.get_session()
        command_client = LifecycleCommandClient(api_session)

        # Install the package
        try:
            response = command_client.install_package(
                workspace=workspace,
                package_file=build_result.package_file,
                force_update=install_config.force,
                variable_bindings=variable_bindings,
                secret_bindings=secret_bindings,
            )
            command_id = response.command_id
            installed_version = build_result.package_version
        except ConflictError as e:
            command_id, existing_version = _handle_install_conflict(
                e, install_config.no_attach, console
            )
            # When we attach to an in-flight install, report the version
            # that's actually installing (from the 409 body) rather than the
            # version we built — the platform ignored ours when it picked up
            # the existing command.
            installed_version = existing_version or build_result.package_version

        progress.update(task, description=f"Command {command_id[:8]}... created")
        progress.remove_task(task)

        # Monitor command events
        console.print()
        final_status = monitor_command_events(
            api_session,
            workspace,
            command_id,
            console,
        )

        success = final_status == "COMPLETED"
        return InstallResult(
            command_id=command_id,
            status=final_status,
            matrix_uri=build_result.package_name,
            version=installed_version,
            artifact_file=build_result.package_file,
            success=success,
            error_message=None if success else f"Command {final_status.lower()}",
        )

    except ValidationError:
        progress.remove_task(task)
        raise
    except Exception as e:
        progress.update(task, description=f"Upload failed: {e}")
        progress.remove_task(task)
        logger.exception("Installation failed")
        return InstallResult(
            command_id="FAILED",
            status="FAILED",
            matrix_uri=build_result.package_name,
            version=build_result.package_version,
            artifact_file=build_result.package_file,
            success=False,
            error_message=str(e),
        )


def _handle_install_conflict(
    exc: ConflictError,
    no_attach: bool,
    console: Console,
) -> tuple[str, str | None]:
    """Decide whether to attach to an in-flight install or fail.

    Implements the CLI's attach behavior: silent attach on same-version conflicts,
    warn-and-attach on version mismatch, exit on ``no_attach=True``. Falls back to re-raising
    the original conflict if the 409 body is missing the structured ``existing.commandId``
    field. Caller is responsible for progress task cleanup.

    Returns ``(command_id, existing_version)`` so callers can populate
    ``InstallResult.version`` with what's actually installing rather than
    the user's pin (which the platform ignored when it attached us to the
    in-flight command). ``existing_version`` is ``None`` when the 409 body
    didn't carry it.

    Shared between the local-build and registry install paths — both surface
    the same 409 envelope, so the same handler picks the right behavior.
    """
    body = exc.body or {}
    existing = body.get("existing") if isinstance(body, dict) else None
    existing_command_id = (
        existing.get("commandId") if isinstance(existing, dict) else None
    )
    existing_version = existing.get("version") if isinstance(existing, dict) else None
    requested = body.get("requested") if isinstance(body, dict) else None
    requested_version = (
        requested.get("version") if isinstance(requested, dict) else None
    )

    if not existing_command_id:
        # 409 without a structured commandId — re-raise; nothing to attach to.
        raise exc

    if no_attach:
        msg = (
            f"Install conflict: an active command {existing_command_id} is in progress"
        )
        if existing_version:
            msg += f" for version {existing_version}"
        msg += " (--no-attach was set)"
        raise ValidationError(msg) from exc

    if requested_version and existing_version and requested_version != existing_version:
        console.print(
            f"[yellow]WARNING:[/yellow] An install of "
            f"v{existing_version} is already in progress "
            f"(command {existing_command_id})."
        )
        console.print(
            f"[yellow]Attaching to existing install. Your requested version "
            f"({requested_version}) was ignored. Use --no-attach to error out instead.[/yellow]"
        )
    else:
        console.print(
            f"Install already in progress (command {existing_command_id}), attaching..."
        )
    return existing_command_id, existing_version


def display_install_results(result: InstallResult, console: Console) -> None:
    """Display installation results.

    Args:
        result: Installation result to display
        console: Rich console for output

    Note: Terminal status messages are handled by command_monitor,
    so we only need minimal output here for non-monitored failures.
    """
    # Only show message for failures that happened before monitoring started
    # (e.g., upload failures). Monitor handles COMPLETED/FAILED status display.
    if not result.success and result.command_id == "FAILED":
        console.print("\n[red]Status: FAILED[/red]")
        if result.error_message:
            console.print(f"[dim]{result.error_message}[/dim]")


def execute_registry_install_workflow(
    progress, install_config: RegistryInstallConfig
) -> InstallResult:
    """Install a published package from the matrix registry.

    Mirrors :func:`execute_install_workflow` for the registry path: resolves
    the target version, fetches its declarations, validates ``--var`` /
    ``--secret-from-env`` flags against those declarations (same flag shape
    and prompt behavior as the local-build path), encrypts secrets, then
    POSTs the install request and tail-follows command events.
    """
    console = Console()

    api_session = config.get_session()
    if not getattr(api_session, "authenticated", False):
        msg = "Not authenticated. Run 'plgt auth login' first."
        raise ValidationError(msg)

    workspace = install_config.workspace
    if not workspace:
        msg = (
            "execute_registry_install_workflow was invoked with no workspace. The CLI "
            "front door is supposed to enforce --workspace; this is a bug."
        )
        raise ValidationError(msg)

    publisher = install_config.publisher
    name = install_config.name

    # Registry reads (/versions, /declarations, /archive) are anonymous on
    # the platform. Forwarding the user's authenticated token to them would
    # cause the platform to 401 those endpoints. Pair an anonymous session
    # with the registry client so the platform sees these as unauthenticated
    # GETs (which it permits).
    from plgt.core.sessions import APISession

    registry_client = RegistryClient(APISession())

    # Resolve the version we'll validate bindings against. The platform
    # ultimately picks the install version, but declarations are version-
    # specific so we need a concrete one to validate up-front. Empty version
    # list indicates an unknown package — surface that clearly rather than
    # letting the install attempt produce a less obvious 404.
    version = install_config.version
    if version is None:
        published = registry_client.get_versions(publisher, name)
        if not published:
            msg = f"No published versions found for {publisher}/{name}"
            raise ValidationError(msg)
        version = published[0]

    declared_vars, declared_secrets = registry_client.get_declarations(
        publisher, name, version
    )
    declarations = RegistryDeclarations(
        variables=list(declared_vars), secrets=list(declared_secrets)
    )

    var_flags = list(install_config.var_flags)
    secret_env_flags = list(install_config.secret_from_env_flags)

    # When the package declares nothing and no flags are passed, skip both
    # the prompt loop and the encryption round trip.
    plain_vars: list = []
    plain_secrets: list = []
    if declarations.variables or declarations.secrets or var_flags or secret_env_flags:
        if not declarations.variables and not declarations.secrets:
            msg = (
                f"--var/--secret-from-env supplied but {publisher}/{name}@{version} "
                "declares no plgt-build:Variable / plgt-scrt:ManagedSecret resources"
            )
            raise ValidationError(msg)
        # Stop the Rich Progress live region while we collect bindings —
        # interactive prompts inside an active Progress context write into
        # the live region and the user sees nothing. Same fix as the
        # local-build path applies (Progress vs typer.prompt collision).
        progress.stop()
        try:
            plain_vars, plain_secrets = collect_bindings(
                declarations,
                var_flags,
                secret_env_flags,
                console=console,
            )
        finally:
            progress.start()

    encrypted_secrets: list[dict] = []
    if plain_secrets:
        console.print(
            f"[dim]Encrypting {len(plain_secrets)} secret binding(s)...[/dim]"
        )
        encrypted = encrypt_secret_bindings(api_session, workspace, plain_secrets)
        encrypted_secrets.extend(
            {
                "uri": esb.uri,
                "keyId": esb.key_id,
                "clientPublicKey": esb.client_public_key,
                "encryptedValue": esb.encrypted_value,
                "nonce": esb.nonce,
            }
            for esb in encrypted
        )

    variable_bindings = [
        {"uri": b.uri, "value": b.value, "sourceMatrix": b.source_matrix}
        for b in plain_vars
    ]

    console.print()
    header_content = (
        f"[bold]Installing:[/bold] {publisher}/{name} v{version} → {workspace}\n"
        "[bold]Source:[/bold]    registry"
    )
    console.print(Panel(header_content, expand=False))
    console.print()

    task = progress.add_task("Submitting install request...", total=None)

    try:
        command_client = LifecycleCommandClient(api_session)
        # ``resolved_version`` falls back to the version we picked locally for
        # declarations validation when we don't have a fresh install response
        # to read it from (i.e. attached to an in-flight install via the 409
        # path). Initialised to the local pick so it's always set.
        resolved_version = version
        try:
            # Pin the install to ``version`` (the version we just fetched
            # declarations against) rather than ``install_config.version``
            # (which may be ``None`` when the user didn't pin). Without this
            # pin the platform would pick its own latest-compatible version,
            # which can disagree with the locally-resolved newest in two
            # cases: (a) ``get_versions`` orders by publishedAt-desc so a
            # backport patch published after a higher minor lands first; and
            # (b) the workspace engine version may make a different version
            # the platform's "latest compatible". Either way, the bindings
            # would have been validated against the wrong version.
            response = command_client.install_from_registry(
                workspace,
                publisher,
                name,
                version=version,
                auto_update=install_config.auto_update,
                variable_bindings=variable_bindings or None,
                secret_bindings=encrypted_secrets or None,
            )
            command_id = response.command_id
            if response.version:
                resolved_version = response.version
        except ConflictError as e:
            command_id, existing_version = _handle_install_conflict(
                e, install_config.no_attach, console
            )
            # On attach, surface the in-flight install's version rather than
            # the one we resolved locally — the platform will install what's
            # already running, not our pin. ``existing_version`` is None on
            # 409 envelopes that don't carry it, in which case we keep the
            # local resolution as the best-effort guess.
            if existing_version:
                resolved_version = existing_version

        progress.update(task, description=f"Command {command_id[:8]}... created")
        progress.remove_task(task)

        console.print()
        final_status = monitor_command_events(
            api_session, workspace, command_id, console
        )

        success = final_status == "COMPLETED"
        # ``artifact_file`` is irrelevant on the registry path (the artifact
        # is fetched server-side by the registry); use a placeholder so the
        # existing ``InstallResult`` shape is preserved.
        return InstallResult(
            command_id=command_id,
            status=final_status,
            matrix_uri=f"{publisher}/{name}",
            version=resolved_version,
            artifact_file=Path(),
            success=success,
            error_message=None if success else f"Command {final_status.lower()}",
        )
    except (ValidationError, ServiceError, ResourceNotFoundError):
        # Let the parent ``plgt install`` command handler render these with
        # consistent formatting (red error message + exit 1). Swallowing
        # them into an ``InstallResult(success=False)`` produced a faint
        # "Status: FAILED" footer instead of the prominent error banner the
        # local-build path gets.
        progress.remove_task(task)
        raise
    except Exception as e:
        progress.update(task, description=f"Install failed: {e}")
        progress.remove_task(task)
        logger.exception("Registry install failed")
        return InstallResult(
            command_id="FAILED",
            status="FAILED",
            matrix_uri=f"{publisher}/{name}",
            version=version,
            artifact_file=Path(),
            success=False,
            error_message=str(e),
        )


__all__ = [
    "create_install_config",
    "display_install_results",
    "execute_install_workflow",
    "execute_registry_install_workflow",
]
