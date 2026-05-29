"""Lifecycle command for Poliglot CLI.

This module implements the main lifecycle CLI commands for build and install operations.
"""

import logging
from pathlib import Path

import typer
from rich.console import Console

from plgt.clients.lifecycle_command_client import LifecycleCommandClient
from plgt.clients.registry_client import RegistryClient
from plgt.core import config, settings
from plgt.core.exceptions import ResourceNotFoundError, ServiceError, ValidationError
from plgt.core.sessions import APISession
from plgt.models.install import RegistryInstallConfig
from plgt.services.build_progress import create_progress_tracker
from plgt.services.command_monitor import monitor_command_events
from plgt.services.deps_install_service import (
    InstallSummary,
    add_dependency_to_config,
    install_local_deps,
    remove_dependency_from_config,
)
from plgt.services.diagnostics import Severity
from plgt.services.install_service import execute_registry_install_workflow
from plgt.services.rdf_operations import BuildError
from plgt.services.validation_pipeline import validate_project
from plgt.services.validation_report import display_validation_report
from plgt.utils.lifecycle import (
    create_build_config,
    create_install_config,
    display_build_results,
    display_install_results,
    execute_build_workflow,
    execute_install_workflow,
)
from plgt.utils.naming import validate_registry_slug
from plgt.utils.workspace_mode import resolve_workspace_mode

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()

# pretty_exceptions_enable=False lets the entrypoint in `plgt.__main__.main`
# intercept CLIError subclasses and render a friendly single-line message
# instead of a rich traceback. Pass `plgt --trace …` to opt back into
# typer's pretty-traceback for diagnosing unexpected failures.
app = typer.Typer(
    help="Lifecycle operations for package management.",
    pretty_exceptions_enable=False,
)


@app.command()
def build(
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to poliglot.yml configuration file. Defaults to poliglot.yml in current directory.",
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Skip the validation pipeline. By default `plgt build` runs the same checks as "
        "`plgt validate` and aborts on errors — use this flag for fast packaging when "
        "you've validated separately or are knowingly building a partial spec.",
    ),
):
    """Build package from RDF specification files."""
    try:
        package_config = create_build_config(config_file)
        console.print(
            f"[blue]Building package: {package_config.name} v{package_config.version}[/blue]",
        )
        console.print(
            f"[dim]  Matrices: {', '.join(m.name for m in package_config.matrices)}[/dim]"
        )

        if not no_validate:
            project_dir = (config_file.parent if config_file else Path.cwd()).resolve()
            console.print("[dim]  Running validation...[/dim]")
            workspace = resolve_workspace_mode(from_workspace=None, from_registry=False)
            result = validate_project(project_dir, workspace=workspace)
            errors = [
                d for d in result.diagnostics.sorted() if d.severity == Severity.ERROR
            ]
            if errors:
                # Show errors (warnings don't block build; surface them with
                # `plgt validate` separately). Cap the dump to keep build
                # output legible; full list is one command away.
                max_shown = 10
                for d in errors[:max_shown]:
                    console.print(f"[red]  {d.code}: {d.message}[/red]")
                if len(errors) > max_shown:
                    console.print(f"[red]  … and {len(errors) - max_shown} more.[/red]")
                console.print(
                    f"[red]Build aborted: {len(errors)} validation error(s). "
                    f"Run `plgt validate` for the full report, or pass "
                    f"`plgt build --no-validate` to skip.[/red]"
                )
                raise typer.Exit(1)

        with create_progress_tracker(console) as progress:
            result = execute_build_workflow(progress, package_config)

        display_build_results(result, console)

    except typer.Exit:
        # Already-handled error path (e.g. validation gate). Don't reformat
        # as "Unexpected error" — the failure was reported cleanly above.
        raise
    except BuildError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        logger.exception("Build failed")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Unexpected error during build: {e}[/red]")
        logger.exception("Unexpected error during build")
        raise typer.Exit(1) from e


def _looks_like_path(target: str) -> bool:
    """Detect path-shaped inputs that should NOT be treated as registry refs.

    Filesystem paths (``./foo``, ``../foo``, ``~/foo``, ``/foo``) and URL-
    shaped strings (``https://...``) all contain ``/`` but obviously aren't
    registry refs. Catching them here lets the install command tell the user
    "this looks like a path; use --config" rather than dying inside the
    registry-ref parser with a confusing slug-format error.
    """
    if not target:
        return False
    if target[0] in (".", "/", "~"):
        return True
    return "://" in target


def _looks_like_registry_ref(target: str) -> bool:
    """Heuristic: a registry ref is ``publisher/name[@version]``.

    A ``/`` is necessary, but the user might also have typed a path or URL
    that happens to contain ``/``. Path/URL detection runs first; anything
    that survives goes to ``_parse_registry_ref``, which then produces a
    specific "publisher must be lowercase alphanumeric…" error if the
    user's segments are malformed.
    """
    return "/" in target and not _looks_like_path(target)


def _parse_registry_ref(ref: str) -> tuple[str, str, str | None]:
    """Parse ``publisher/name[@version]`` into its components.

    The ``@version`` suffix is optional; when absent (or when literally
    ``@latest``) the CLI fetches the package's published versions and
    validates bindings against the newest. ``@latest`` is sugar for
    "no specific pin" — the platform picks the latest compatible version
    server-side.
    """
    if "/" not in ref:
        msg = f"package ref '{ref}' must be in 'publisher/name[@version]' form"
        raise ValidationError(msg)
    publisher, _, rest = ref.partition("/")
    if not publisher:
        msg = f"package ref '{ref}' has empty publisher"
        raise ValidationError(msg)
    name, sep, version = rest.partition("@")
    if not name:
        msg = f"package ref '{ref}' has empty package name"
        raise ValidationError(msg)
    if sep and not version:
        msg = f"package ref '{ref}' has '@' but no version"
        raise ValidationError(msg)
    # ``latest`` is the conventional "I just want the newest" sentinel — npm,
    # docker, cargo all accept it. Normalize to None so the downstream
    # workflow takes the unpinned path.
    if version == "latest":
        version = ""
    # Match the validation the registry will run server-side so a typo
    # (uppercase, slash, etc.) errors locally with a clear message instead of
    # failing as a 400 after the network round trip.
    try:
        validate_registry_slug("publisher", publisher)
        validate_registry_slug("package name", name)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    return publisher, name, version or None


@app.command()
def install(
    target: str | None = typer.Argument(
        None,
        help=(
            "Optional 'publisher/name[@version]' to install from the matrix "
            "registry into the workspace. Omit to build and install the local "
            "project package."
        ),
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to poliglot.yml (local-build path only). Defaults to "
            "poliglot.yml in the current directory."
        ),
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Target workspace slug. Required: `plgt install` does not fall "
            "back to a default workspace because pushing to the wrong "
            "workspace is the high-cost surprise."
        ),
    ),
    release_notes: str | None = typer.Option(
        None,
        "--release-notes",
        help=(
            "Release notes for the installation. Local-build path only — "
            "ignored with a warning on registry installs."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Force update even if validation fails. Local-build path only — "
            "ignored with a warning on registry installs (the platform "
            "auto-detects re-installs and bypasses the gate itself)."
        ),
    ),
    var: list[str] = typer.Option(
        None,
        "--var",
        help=(
            "Bind a plgt-build:Variable in REF=VALUE form (repeatable). "
            "Local-build: REF is a full URI or 'prefix:localName' QName resolved "
            "against the project's matrix TTL prefixes. Registry: REF is a full "
            "URI or an unambiguous localName matching one declared variable."
        ),
    ),
    secret_from_env: list[str] = typer.Option(
        None,
        "--secret-from-env",
        help=(
            "Bind a plgt-scrt:ManagedSecret in REF=ENV_VAR form (repeatable). "
            "Reads the value from the named environment variable and encrypts "
            "client-side before sending."
        ),
    ),
    no_attach: bool = typer.Option(
        False,
        "--no-attach",
        help=(
            "Exit with an error if an install for this package is already in "
            "progress, instead of silently attaching to it. Works on both paths."
        ),
    ),
    auto_update: bool | None = typer.Option(
        None,
        "--auto-update/--no-auto-update",
        help=(
            "Registry-install only. Sets the autoUpdate flag on the resulting "
            "PackageInstallation: enabled means future patch versions auto-"
            "install, disabled means they don't. Defaults to disabled when "
            "omitted; system packages (e.g. poliglot/os) ignore the flag and "
            "are always treated as auto-update on by the platform. Ignored "
            "with a warning on local-build installs."
        ),
    ),
):
    """Install a package to a workspace.

    Two invocation shapes, both requiring ``--workspace <slug>`` explicitly
    (no default fallback because pushing to the wrong workspace is the
    high-cost surprise):

    * ``plgt install --workspace <slug>``: build and push the local package
      (``poliglot.yml``) to the named workspace. Honours ``--var``,
      ``--secret-from-env``, ``--config``, ``--release-notes``, ``--force``,
      and ``--no-attach``.
    * ``plgt install <publisher>/<name>[@<version>] --workspace <slug>``:
      tell the workspace to install the published registry package.
      ``--auto-update`` is honoured.

    To populate the local dependency cache, use ``plgt sync``. To add a
    dependency to ``poliglot.yml``, use ``plgt add``.
    """
    try:
        is_registry_target = target is not None and _looks_like_registry_ref(target)

        # Local-deps semantics moved to `plgt sync` / `plgt add`. Catch the legacy
        # invocations here and point users at the new verbs rather than running a stale
        # behaviour.
        if workspace is None:
            if target is None:
                msg = (
                    "plgt install requires --workspace <slug>. To populate the local "
                    "dependency cache, use `plgt sync`."
                )
                raise ValidationError(msg)
            if is_registry_target:
                msg = (
                    f"plgt install {target} requires --workspace <slug>. To add the "
                    f"package to poliglot.yml dependencies and sync, use "
                    f"`plgt add {target}`."
                )
                raise ValidationError(msg)
            _raise_unrecognised_target(target)

        # --workspace path: existing workspace-install behaviour.
        if is_registry_target:
            _warn_unused_flags_for_registry_path(
                config_file=config_file,
                release_notes=release_notes,
                force=force,
            )
            _run_registry_install(
                target=target,
                workspace=workspace,
                var_flags=tuple(var or ()),
                secret_from_env_flags=tuple(secret_from_env or ()),
                auto_update=auto_update,
                no_attach=no_attach,
            )
            return

        if target is not None:
            _raise_unrecognised_target(target)

        _warn_unused_flags_for_local_path(auto_update=auto_update)

        install_config = create_install_config(
            config_file,
            workspace,
            release_notes,
            force,
            var_flags=tuple(var or ()),
            secret_from_env_flags=tuple(secret_from_env or ()),
            no_attach=no_attach,
        )

        console.print(f"Installing package to workspace '{workspace}'")

        with create_progress_tracker(console) as progress:
            result = execute_install_workflow(progress, install_config)

        display_install_results(result, console)

    except typer.Exit:
        raise
    except ValidationError as e:
        console.print(f"[red]error: {e}[/red]")
        raise typer.Exit(1) from e
    except ResourceNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Install failed: {e}[/red]")
        logger.debug("Install failed", exc_info=True)
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Install failed: {e}[/red]")
        logger.debug("Install failed", exc_info=True)
        raise typer.Exit(1) from e


def _raise_unrecognised_target(target: str) -> None:
    if _looks_like_path(target):
        msg = (
            f"'{target}' looks like a filesystem path or URL, not a "
            "registry ref. Use --config for a custom poliglot.yml path; "
            "omit the positional argument to install from the project in "
            "the current directory."
        )
    else:
        msg = (
            f"target '{target}' is not a recognised registry ref "
            "('publisher/name[@version]') and no other install target "
            "shape is supported"
        )
    raise ValidationError(msg)


@app.command()
def sync(
    update: bool = typer.Option(
        False,
        "--update",
        help=(
            "Re-resolve every entry against the current source-of-truth "
            "(workspace or registry) and rewrite the lockfile. Cache "
            "markers are ignored so packages are re-fetched."
        ),
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help=(
            "Sync against the named workspace's installed packages. Pins "
            "each declared dep to the version the workspace has; falls "
            "back to registry-resolve for deps the workspace doesn't have "
            "(emits PLGT_W0902 to flag the pre-push gap). Mutually "
            "exclusive with --from-registry. Defaults to the configured "
            "default workspace when neither flag is supplied."
        ),
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help=(
            "Sync from the public registry only (anonymous). Bypasses any "
            "default workspace and resolves each dep to the latest version "
            "compatible with the project's engineVersion. Mutually "
            "exclusive with --from-workspace. Use for OSS-repo CI where "
            "the lockfile must be reproducible without workspace state."
        ),
    ),
) -> None:
    """Sync local dependency cache from poliglot.yml.

    Reads ``engineVersion`` and ``dependencies:`` from ``poliglot.yml``,
    resolves everything transitively, and populates the per-mode cache
    subtree under ``.matrix/deps/``. Writes a per-mode lockfile so
    subsequent runs are deterministic.

    To add a new dependency, use ``plgt add``. To deploy to a workspace,
    use ``plgt install --workspace <slug>``.
    """
    try:
        from plgt.utils.workspace_mode import resolve_workspace_mode

        resolved_workspace = resolve_workspace_mode(
            from_workspace=from_workspace, from_registry=from_registry
        )
        if resolved_workspace is None and not from_registry:
            console.print(
                "[dim]No default workspace configured; syncing dependencies from the "
                "registry. Set a default with `plgt auth sync` for workspace-sync mode, "
                "or pass --from-workspace <slug>.[/dim]"
            )
        _run_local_deps_install(
            target=None,
            no_save=False,
            update=update,
            workspace=resolved_workspace,
        )
    except typer.Exit:
        raise
    except ValidationError as e:
        console.print(f"[red]error: {e}[/red]")
        raise typer.Exit(1) from e
    except ResourceNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Sync failed: {e}[/red]")
        logger.debug("Sync failed", exc_info=True)
        raise typer.Exit(1) from e


@app.command()
def add(
    target: str = typer.Argument(
        ...,
        help=(
            "Registry ref 'publisher/name[@version]' to add to "
            "poliglot.yml's dependencies. Omit @version to pin to the "
            "latest compatible version's minor floor."
        ),
    ),
    no_sync: bool = typer.Option(
        False,
        "--no-sync",
        help=(
            "Skip the local cache sync that normally follows. Useful when "
            "batching multiple adds before a single sync."
        ),
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help=(
            "Sync mode to use after adding. Same semantics as `plgt sync "
            "--from-workspace`. Ignored when --no-sync is passed."
        ),
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help=(
            "Sync mode to use after adding. Same semantics as `plgt sync "
            "--from-registry`. Ignored when --no-sync is passed."
        ),
    ),
) -> None:
    """Add a dependency to poliglot.yml and sync.

    Writes ``publisher/name`` to ``poliglot.yml``'s ``dependencies:`` map
    with a minor-floor range derived from the resolved version (e.g.
    ``">=1.5 <2"`` when latest is 1.5.x). Then triggers a sync to
    populate the local cache, unless ``--no-sync`` is passed.
    """
    try:
        from plgt.utils.workspace_mode import resolve_workspace_mode

        publisher, name, version = _parse_registry_ref(target)  # validates shape early
        del publisher, name  # _run_local_deps_install re-parses for the same effect

        if no_sync:
            # Manifest-only mutation: write a concrete minor-floor range to poliglot.yml
            # without touching the cache. The user MUST supply @version — without one we
            # would write the ">=0" sentinel that only the post-sync rewrite knows how to
            # tighten. Phase A explicitly removed that sentinel as a persisted state; the
            # --no-sync branch must not reintroduce it.
            if version is None:
                msg = (
                    "plgt add --no-sync requires an explicit @version (e.g. "
                    f"`plgt add {target}@1.5.0 --no-sync`). Without --no-sync the "
                    "resolver picks the latest compatible version and writes a "
                    "minor-floor range; with --no-sync there is no resolver run, so "
                    "the version must be pinned by hand."
                )
                raise ValidationError(msg)
            project_dir = Path.cwd()
            config_path = project_dir / "poliglot.yml"
            if not config_path.exists():
                msg = f"No poliglot.yml in {project_dir}. Run `plgt init` first."
                raise ValidationError(msg)
            pub, nm, ver = _parse_registry_ref(target)
            version_range = _range_from_version_arg(ver)
            add_dependency_to_config(
                config_path,
                publisher=pub,
                name=nm,
                version_range=version_range,
            )
            console.print(
                f"[green]Added {pub}/{nm} {version_range} to poliglot.yml "
                "(skipped sync; run `plgt sync` to populate the cache).[/green]"
            )
            return

        resolved_workspace = resolve_workspace_mode(
            from_workspace=from_workspace, from_registry=from_registry
        )
        if resolved_workspace is None and not from_registry:
            console.print(
                "[dim]No default workspace configured; syncing dependencies from the "
                "registry. Set a default with `plgt auth sync` for workspace-sync mode, "
                "or pass --from-workspace <slug>.[/dim]"
            )
        _run_local_deps_install(
            target=target,
            no_save=False,
            update=False,
            workspace=resolved_workspace,
        )
    except typer.Exit:
        raise
    except ValidationError as e:
        console.print(f"[red]error: {e}[/red]")
        raise typer.Exit(1) from e
    except ResourceNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Add failed: {e}[/red]")
        logger.debug("Add failed", exc_info=True)
        raise typer.Exit(1) from e


@app.command()
def remove(
    target: str = typer.Argument(
        ...,
        help="Registry ref 'publisher/name' to remove from poliglot.yml's dependencies.",
    ),
    no_sync: bool = typer.Option(
        False,
        "--no-sync",
        help="Skip the local cache sync that normally follows.",
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help="Sync mode for the post-remove sync. Same semantics as `plgt sync`.",
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help="Sync mode for the post-remove sync. Same semantics as `plgt sync`.",
    ),
) -> None:
    """Remove a dependency from poliglot.yml and sync.

    Drops ``publisher/name`` from ``poliglot.yml``'s ``dependencies:``
    map, then triggers a sync to prune the cache, unless ``--no-sync``
    is passed.
    """
    try:
        from plgt.utils.workspace_mode import resolve_workspace_mode

        publisher, name, _ = _parse_registry_ref(target)
        project_dir = Path.cwd()
        config_path = project_dir / "poliglot.yml"
        if not config_path.exists():
            msg = f"No poliglot.yml in {project_dir}."
            raise ValidationError(msg)

        removed = remove_dependency_from_config(
            config_path, publisher=publisher, name=name
        )
        if not removed:
            msg = f"{publisher}/{name} is not in poliglot.yml's dependencies."
            raise ValidationError(msg)
        console.print(f"[green]Removed {publisher}/{name} from poliglot.yml[/green]")

        if no_sync:
            console.print(
                "[dim]Skipped sync; run `plgt sync` to update the cache.[/dim]"
            )
            return

        resolved_workspace = resolve_workspace_mode(
            from_workspace=from_workspace, from_registry=from_registry
        )
        _run_local_deps_install(
            target=None,
            no_save=False,
            update=False,
            workspace=resolved_workspace,
        )
    except typer.Exit:
        raise
    except ValidationError as e:
        console.print(f"[red]error: {e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Remove failed: {e}[/red]")
        logger.debug("Remove failed", exc_info=True)
        raise typer.Exit(1) from e


def _run_local_deps_install(
    *,
    target: str | None,
    no_save: bool,
    update: bool = False,
    workspace: str | None = None,
) -> None:
    """Drive the local-deps install path. Shared between ``plgt sync`` and
    ``plgt add``.

    If ``target`` is set, the package is added to ``poliglot.yml``'s
    ``dependencies:`` (unless ``--no-sync``) before resolving. With
    ``--no-sync``, the target is still installed for this run as a transient
    root dep, so the cache is populated but ``poliglot.yml`` stays
    untouched. Then the full project is resolved and the cache populated.

    The yml write is deferred until AFTER the install completes
    successfully — a registry failure mid-install must not leave a yml
    entry referencing a package that never landed in the cache.
    """
    from plgt.models.build_types import PackageDependency

    project_dir = Path.cwd()
    config_path = project_dir / "poliglot.yml"

    transient_deps: list[PackageDependency] = []
    pending_yml_write: tuple[str, str, str] | None = None

    if target is not None:
        publisher, name, version = _parse_registry_ref(target)
        version_range = _range_from_version_arg(version)
        if no_save:
            transient_deps.append(
                PackageDependency(
                    publisher=publisher, name=name, version_range=version_range
                )
            )
            console.print(
                f"[dim]Installing {publisher}/{name} {version_range} (transient, --no-sync)[/dim]"
            )
        else:
            if not config_path.exists():
                msg = (
                    f"No poliglot.yml in {project_dir}. Run `plgt init` "
                    "first, or use --no-sync to skip the yml write."
                )
                raise ValidationError(msg)
            # Treat target as a transient for the install; persist to yml
            # only after the install succeeds. Avoids partial-failure
            # corruption (yml says we depend on X, lockfile/cache disagree).
            transient_deps.append(
                PackageDependency(
                    publisher=publisher, name=name, version_range=version_range
                )
            )
            pending_yml_write = (publisher, name, version_range)

    # Registry reads are anonymous; the API rejects forwarded user tokens on these
    # endpoints, so the RegistryClient uses an unauthed session.
    # Workspace-packages lookup (workspace-sync mode) requires auth and uses the profile.
    registry_client = RegistryClient(APISession())
    workspace_packages_client = None
    if workspace is not None:
        from plgt.clients.workspace_packages_client import WorkspacePackagesClient

        workspace_packages_client = WorkspacePackagesClient(config.get_session())

    def _on_workspace_fallback(publisher: str, name: str, version_range: str) -> None:
        # PLGT_W0902: the workspace was syncable but did not have this dep. The CLI fell back
        # to registry-resolve for it, which mirrors what the platform would do at push time.
        # Surface the pre-push gap so the author can install on the workspace before pushing.
        # `>=0` is the synthetic "no constraint" sentinel used when `plgt add` is invoked
        # without an @version — surfacing it in the warning reads like a user typo, so we
        # only include the range when it's a real declared constraint.
        coord = f"{publisher}/{name}"
        if version_range and version_range != ">=0":
            coord = f"{coord} {version_range}"
        console.print(
            f"[yellow]PLGT_W0902:[/yellow] '{coord}' is not installed on workspace "
            f"'{workspace}'; resolved from registry. Run "
            f"`plgt install {publisher}/{name} --workspace {workspace}` to activate it "
            "on the workspace before pushing."
        )

    def _on_lockfile_drift(
        publisher: str, name: str, pinned_version: str, declared_range: str
    ) -> None:
        # PLGT_W0901: the prior lockfile pinned a version that no longer satisfies the
        # declared range. Surface so the author understands the install re-resolved instead
        # of fast-pathing on the pin.
        console.print(
            f"[yellow]PLGT_W0901:[/yellow] '{publisher}/{name}' was pinned at "
            f"{pinned_version} in the lockfile, but that does not satisfy declared range "
            f"'{declared_range}'. Re-resolving."
        )

    def _on_workspace_range_mismatch(
        publisher: str, name: str, workspace_version: str, declared_range: str
    ) -> None:
        # PLGT_W0903: workspace has the dep at a version that doesn't satisfy yml. The
        # workspace wins (it's the deployment target) but the author should know yml drifted.
        console.print(
            f"[yellow]PLGT_W0903:[/yellow] workspace '{workspace}' has '{publisher}/{name}' "
            f"at {workspace_version}, which does not satisfy declared range "
            f"'{declared_range}'. Using the workspace version (it is the deployment "
            "target); update the range in poliglot.yml or upgrade the workspace."
        )

    def _on_pin_vs_workspace(
        publisher: str, name: str, pinned_version: str, workspace_version: str
    ) -> None:
        # PLGT_W0904: lockfile pin is being honored, but the workspace has a different
        # version installed. Both satisfy the declared range, but validation will not match
        # what gets deployed. Author can refresh with `plgt sync --update`.
        console.print(
            f"[yellow]PLGT_W0904:[/yellow] lockfile pins '{publisher}/{name}' at "
            f"{pinned_version}, but workspace '{workspace}' has {workspace_version}. "
            "Validation will use the pinned version; the workspace will deploy something "
            "different. Run `plgt sync --update` to refresh against workspace state."
        )

    summary = install_local_deps(
        project_dir,
        registry_client,
        transient_deps=transient_deps or None,
        update=update,
        workspace=workspace,
        workspace_packages_client=workspace_packages_client,
        workspace_drift_notifier=_on_workspace_fallback if workspace else None,
        lockfile_drift_notifier=_on_lockfile_drift,
        workspace_range_mismatch_notifier=(
            _on_workspace_range_mismatch if workspace else None
        ),
        pin_vs_workspace_notifier=_on_pin_vs_workspace if workspace else None,
    )

    if pending_yml_write is not None:
        publisher, name, version_range = pending_yml_write
        # When the user did not pin a version, the early-write range is the permissive sentinel
        # (">=0"). After install the resolver has chosen a concrete version; rewrite the recorded
        # range to its minor-floor so future `plgt sync --update` keeps the same minor unless
        # the user explicitly bumps. This is npm's caret-on-minor semantics, which avoids
        # surprise major upgrades.
        if version_range == ">=0":
            resolved = next(
                (
                    d
                    for d in summary.dependencies
                    if d.publisher == publisher and d.name == name
                ),
                None,
            )
            if resolved is None:
                # We just installed this dep; the summary MUST contain it. A missing entry
                # means either the resolver dropped it silently or the dep list shape changed.
                # Per the user's fail-fast philosophy, refuse to persist the floating sentinel
                # so the failure mode surfaces immediately rather than corrupting poliglot.yml.
                msg = (
                    f"Resolver did not report a resolved version for {publisher}/{name}; "
                    "refusing to persist a permissive '>=0' range to poliglot.yml. "
                    "This indicates a bug in the install summary."
                )
                raise ServiceError(msg)
            version_range = _range_from_version_arg(resolved.version)
        add_dependency_to_config(
            config_path,
            publisher=publisher,
            name=name,
            version_range=version_range,
        )
        console.print(
            f"[green]Added {publisher}/{name} {version_range} to poliglot.yml[/green]"
        )

    _display_local_deps_summary(summary)


def _range_from_version_arg(version: str | None) -> str:
    """Derive a semver range to record in poliglot.yml from the user-supplied
    @version. Default policy is minor-floor (``>=X.Y <X+1``); a bare major
    becomes ``>=X <X+1``.

    A ``None`` (no ``@version``) returns the permissive ``">=0"`` sentinel so
    the resolver can pick the latest compatible version. The caller is
    responsible for rewriting the yml-bound range to the resolved version's
    minor-floor after install completes; storing ``">=0"`` permanently would
    let every future install float across majors, which is not the policy.
    """
    if version is None or version == "latest":
        return ">=0"
    parts = version.split(".")
    try:
        major = int(parts[0])
    except ValueError as e:
        msg = f"Invalid version '{version}' in registry ref"
        raise ValidationError(msg) from e
    if len(parts) >= 2:
        try:
            minor = int(parts[1])
        except ValueError as e:
            msg = f"Invalid version '{version}' in registry ref"
            raise ValidationError(msg) from e
        return f">={major}.{minor} <{major + 1}"
    return f">={major} <{major + 1}"


def _display_local_deps_summary(summary: "InstallSummary") -> None:
    """Render the local-deps install summary on the console."""
    console.print(f"[green]Engine:[/green] poliglot/os@{summary.engine.version}")
    if summary.dependencies:
        console.print("[green]Dependencies:[/green]")
        for dep in summary.dependencies:
            tag = "" if dep.root else f" (via {dep.via})"
            console.print(f"  - {dep.publisher}/{dep.name}@{dep.version}{tag}")
    else:
        console.print("[dim]No dependencies declared.[/dim]")
    console.print(
        f"[dim]Fetched: {len(summary.fetched)}, cached: {len(summary.cached)}[/dim]"
    )
    console.print(f"[dim]Lockfile: {summary.lockfile_path}[/dim]")


def _run_registry_install(
    *,
    target: str,
    workspace: str | None,
    var_flags: tuple[str, ...],
    secret_from_env_flags: tuple[str, ...],
    auto_update: bool | None,
    no_attach: bool,
) -> None:
    """Drive the registry-install path of ``plgt install`` and exit on failure.

    Surfaces ``ValidationError``/``ResourceNotFoundError`` via the parent
    ``install`` command's exception handlers (they map to exit 1 with a
    coloured message).
    """
    publisher, name, version = _parse_registry_ref(target)

    install_config = RegistryInstallConfig(
        publisher=publisher,
        name=name,
        version=version,
        workspace=workspace,
        var_flags=var_flags,
        secret_from_env_flags=secret_from_env_flags,
        auto_update=auto_update,
        no_attach=no_attach,
    )

    with create_progress_tracker(console) as progress:
        result = execute_registry_install_workflow(progress, install_config)

    display_install_results(result, console)
    if not result.success:
        raise typer.Exit(1)


def _warn_unused_flags_for_registry_path(
    *,
    config_file: Path | None,
    release_notes: str | None,
    force: bool,
) -> None:
    """Warn the user that local-build-only flags do nothing on a registry install.

    Warn-and-continue rather than reject — a user re-running an old script
    against a new install target shouldn't get a hard error for harmless
    leftovers.
    """
    if config_file is not None:
        console.print(
            "[yellow]warning:[/yellow] --config is ignored on registry installs."
        )
    if release_notes is not None:
        console.print(
            "[yellow]warning:[/yellow] --release-notes is ignored on registry "
            "installs (release notes belong to the published package itself)."
        )
    if force:
        console.print(
            "[yellow]warning:[/yellow] --force is ignored on registry installs "
            "(the platform auto-detects re-installs and bypasses the gate)."
        )


def _warn_unused_flags_for_local_path(*, auto_update: bool | None) -> None:
    """Warn the user that registry-only flags do nothing on a local-build install."""
    if auto_update is not None:
        console.print(
            "[yellow]warning:[/yellow] --auto-update is ignored on local-build "
            "installs (the autoUpdate flag is only meaningful for "
            "registry-sourced PackageInstallations)."
        )


@app.command()
def uninstall(
    package_name: str = typer.Argument(..., help="The package name to uninstall."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
):
    """Uninstall a package from your workspace."""
    try:
        # Resolve workspace
        target_workspace = workspace
        if not target_workspace:
            target_workspace = config.defaults.get("workspace")
            if not target_workspace:
                console.print(
                    "[red]No workspace specified. Use --workspace or run 'plgt auth sync' to configure a default.[/red]"
                )
                raise typer.Exit(1)

        # Get authenticated session
        session = config.get_session()
        if not session.authenticated:
            console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
            raise typer.Exit(1)

        # Confirmation prompt
        if not yes:
            confirm = typer.confirm(
                f"Uninstall package '{package_name}' from workspace '{target_workspace}'?"
            )
            if not confirm:
                console.print("[dim]Cancelled.[/dim]")
                raise typer.Exit(0)

        # Send uninstall request
        client = LifecycleCommandClient(session)
        response = client.uninstall_package(target_workspace, package_name)
        console.print(
            f"[blue]Uninstall initiated for '{package_name}' (command {response.command_id})[/blue]"
        )

        # Monitor command events until terminal status
        console.print()
        final_status = monitor_command_events(
            session,
            target_workspace,
            response.command_id,
            console,
        )

        if final_status == "COMPLETED":
            console.print(
                f"\n[green]Package '{package_name}' has been uninstalled.[/green]"
            )
        elif final_status == "FAILED":
            console.print(
                f"\n[red]Uninstall failed for '{package_name}'. Check the error details above.[/red]"
            )
            raise typer.Exit(1)

    except ResourceNotFoundError as e:
        console.print(
            f"[red]Package '{package_name}' is not installed in workspace '{target_workspace}'.[/red]"
        )
        raise typer.Exit(1) from e
    except ValidationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Failed to uninstall: {e}[/red]")
        logger.exception("Failed to uninstall package")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error during uninstall")
        raise typer.Exit(1) from e


@app.command(name="set-auto-update")
def set_auto_update(
    package_name: str = typer.Argument(..., help="The installed package name."),
    auto_update: bool = typer.Argument(
        ...,
        help="New auto-update flag (true to enable patch auto-install, false to disable).",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
):
    """Toggle whether a PackageInstallation auto-installs patch versions."""
    try:
        target_workspace = workspace
        if not target_workspace:
            target_workspace = config.defaults.get("workspace")
            if not target_workspace:
                console.print(
                    "[red]No workspace specified. Use --workspace or run 'plgt auth sync' to configure a default.[/red]"
                )
                raise typer.Exit(1)

        session = config.get_session()
        if not session.authenticated:
            console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
            raise typer.Exit(1)

        client = LifecycleCommandClient(session)
        updated = client.set_auto_update(target_workspace, package_name, auto_update)
        console.print(
            f"[green]auto-update={'on' if auto_update else 'off'} for '{package_name}' in '{target_workspace}'.[/green]"
        )
        current_version = (
            updated.get("currentVersion") if isinstance(updated, dict) else None
        )
        if current_version:
            console.print(f"[dim]  currentVersion: {current_version}[/dim]")

    except ResourceNotFoundError as e:
        console.print(
            f"[red]Package '{package_name}' is not installed in workspace '{target_workspace}'.[/red]"
        )
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Failed to set auto-update: {e}[/red]")
        logger.exception("Failed to set auto-update")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error during set-auto-update")
        raise typer.Exit(1) from e


@app.command(name="list-lifecycle-commands")
def list_lifecycle_commands(
    package_name: str = typer.Argument(
        ..., help="The package name to list lifecycle commands for."
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Maximum number of commands to show.",
    ),
):
    """List lifecycle commands for a package."""
    try:
        # Resolve workspace
        target_workspace = workspace
        if not target_workspace:
            target_workspace = config.defaults.get("workspace")
            if not target_workspace:
                console.print(
                    "[red]No workspace specified. Use --workspace or run 'plgt auth sync' to configure a default.[/red]"
                )
                raise typer.Exit(1)

        # Get authenticated session
        session = config.get_session()
        if not session.authenticated:
            console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
            raise typer.Exit(1)

        # Fetch commands
        client = LifecycleCommandClient(session)
        commands = client.list_commands(target_workspace, package_name, size=limit)

        if not commands:
            console.print(
                f"[yellow]No commands found for package '{package_name}'.[/yellow]"
            )
            raise typer.Exit(0)

        console.print(
            f"[bold]Commands for '{package_name}' in workspace '{target_workspace}':[/bold]\n"
        )

        for d in commands:
            status_color = {
                "COMPLETED": "green",
                "FAILED": "red",
                "IN_PROGRESS": "yellow",
                "PENDING": "dim",
            }.get(d.status.value, "white")

            console.print(f"  [bold]{d.id}[/bold]")
            console.print(f"    Version: {d.version}")
            console.print(
                f"    Status:  [{status_color}]{d.status.value}[/{status_color}]"
            )
            console.print(f"    Created: {d.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
            if d.error_message:
                console.print(f"    Error:   [red]{d.error_message}[/red]")
            console.print()

    except ServiceError as e:
        console.print(f"[red]Failed to list commands: {e}[/red]")
        logger.exception("Failed to list commands")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error listing commands")
        raise typer.Exit(1) from e


@app.command(name="get-lifecycle-validation-report")
def get_lifecycle_validation_report(
    command_id: str | None = typer.Argument(
        None,
        help="The command ID to get the validation report for. Required unless --latest is used.",
    ),
    package_name: str | None = typer.Option(
        None,
        "--package",
        "-p",
        help="Package name. Required when using --latest.",
    ),
    latest: bool = typer.Option(
        False,
        "--latest",
        "-l",
        help="Get the validation report for the latest command of the package.",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    formatted: bool = typer.Option(
        False,
        "--formatted",
        "-f",
        help="Display formatted report instead of raw Turtle.",
    ),
):
    """Get the SHACL validation report for a command."""
    try:
        # Validate arguments
        if latest and not package_name:
            console.print("[red]--package is required when using --latest[/red]")
            raise typer.Exit(1)

        if not latest and not command_id:
            console.print(
                "[red]Either provide a command ID or use --latest with --package[/red]"
            )
            raise typer.Exit(1)

        # Resolve workspace
        target_workspace = workspace
        if not target_workspace:
            target_workspace = config.defaults.get("workspace")
            if not target_workspace:
                console.print(
                    "[red]No workspace specified. Use --workspace or run 'plgt auth sync' to configure a default.[/red]"
                )
                raise typer.Exit(1)

        # Get authenticated session
        session = config.get_session()
        if not session.authenticated:
            console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
            raise typer.Exit(1)

        client = LifecycleCommandClient(session)

        # Resolve command ID if using --latest
        target_command_id = command_id
        if latest:
            console.print(
                f"[dim]Fetching latest command for package '{package_name}'...[/dim]"
            )
            commands = client.list_commands(target_workspace, package_name, size=1)
            if not commands:
                console.print(
                    f"[yellow]No commands found for package '{package_name}'.[/yellow]"
                )
                raise typer.Exit(0)
            target_command_id = str(commands[0].id)
            console.print(f"[dim]Latest command: {target_command_id}[/dim]")

        # Fetch validation report
        console.print("[dim]Fetching validation report...[/dim]")
        report_ttl = client.get_validation_report(target_workspace, target_command_id)

        if report_ttl is None:
            console.print(
                "[yellow]No validation report found for this command.[/yellow]"
            )
            raise typer.Exit(0)

        if formatted:
            # Display formatted report
            display_validation_report(report_ttl, console)
        else:
            # Output raw TTL to stdout (no Rich formatting)
            console.out(report_ttl)

    except ServiceError as e:
        console.print(f"[red]Failed to fetch validation report: {e}[/red]")
        logger.exception("Failed to fetch validation report")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error fetching validation report")
        raise typer.Exit(1) from e
