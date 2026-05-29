"""Publish, yank, and unyank commands for the public matrix registry.

These commands target the authenticated publisher associated with the caller's
account — the publisher slug is never accepted as a user flag. It's discovered
via ``GET /publishers/me`` and embedded into the publish/yank URL automatically,
so a misconfigured CLI can't accidentally publish under someone else's slug.

End-to-end flow for ``plgt publish``:
    1. Resolve build config from ``poliglot.yml``.
    2. Build the package locally (same path as ``plgt build``).
    3. Resolve caller's publisher.
    4. POST the resulting tarball to the registry's publish endpoint.
    5. Render the structured 422 / 409 / 403 / 429 responses cleanly.

``--dry-run`` runs the same validation server-side but skips artifact upload
and persistence. Use it before a real publish to catch boundary / SHACL /
namespace violations without spending a version slot.

``plgt yank <name>@<version> --reason "..."`` and ``plgt unyank <name>@<version>``
operate on the caller's own publisher's packages — no cross-publisher mutation
surface is exposed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path  # noqa: TC003 — required at runtime by typer's annotation eval
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from plgt.clients.publish_client import PublishClient
from plgt.core import config, settings
from plgt.core.exceptions import (
    AuthenticationError,
    ConflictError,
    ResourceNotFoundError,
    ServiceError,
    ValidationError,
)
from plgt.services.build_progress import create_progress_tracker
from plgt.services.rdf_operations import BuildError
from plgt.utils.lifecycle import create_build_config, execute_build_workflow

# NOTE: `Path` must be imported at runtime, not under TYPE_CHECKING. typer
# evaluates the `Path | None` annotation on the publish command callback via
# inspect.signature(eval_str=True), so the symbol has to resolve in the module
# namespace at runtime — ruff's TC003 suggestion is incorrect for this file.

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()

app = typer.Typer(help="Registry publish, yank, and unyank operations.")


# ----- helpers -----


def _get_client() -> PublishClient:
    """Resolve an authenticated PublishClient or exit with a clear message."""
    session = config.get_session()
    if not session.authenticated:
        console.print("[red]Not authenticated. Run `plgt auth login` first.[/red]")
        raise typer.Exit(1)
    return PublishClient(session)


# Parses ``<name>@<version>``. Used by yank/unyank where the caller's publisher
# is implicit and only the local name+version need to be parsed from the
# positional argument. The regex matches the registry's own slug shape so users
# get a clear error rather than a 400 from the server later.
_NAME_AT_VERSION_REGEX = re.compile(r"^([a-z0-9][a-z0-9-]*)@(.+)$")


def _parse_name_at_version(spec: str) -> tuple[str, str]:
    """Parse ``<name>@<version>``. Caller's publisher is implicit."""
    match = _NAME_AT_VERSION_REGEX.match(spec)
    if not match:
        console.print(
            f"[red]Invalid package reference: {spec!r}. Use `<name>@<version>`.[/red]"
        )
        raise typer.Exit(2)
    return match.group(1), match.group(2)


def _render_validation_report(report: dict[str, Any] | None) -> None:
    """Render a structured 422 ValidationReport from the platform.

    The server's :class:`ValidationReport` carries a list of violations with
    ``rule``, ``message``, and optional ``suggestion``. We print one row per
    violation so publishers can spot the offending boundary rule at a glance.
    """
    if not isinstance(report, dict):
        return
    violations = report.get("violations") or []
    warnings = report.get("warnings") or []
    if violations:
        table = Table(title="Validation violations", show_lines=True)
        table.add_column("Rule", style="red")
        table.add_column("Message")
        table.add_column("Suggestion", style="dim")
        for v in violations:
            table.add_row(
                v.get("rule", "?"),
                v.get("message", ""),
                v.get("suggestion") or "",
            )
        console.print(table)
    if warnings:
        table = Table(title="Validation warnings", show_lines=False)
        table.add_column("Rule", style="yellow")
        table.add_column("Message")
        for w in warnings:
            table.add_row(w.get("rule", "?"), w.get("message", ""))
        console.print(table)


# ----- publish -----


@app.command()
def publish(
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to poliglot.yml. Defaults to poliglot.yml in the current directory.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run all validation server-side without uploading or persisting.",
    ),
) -> None:
    """Build the package and publish it to the registry under the caller's publisher."""
    try:
        package_config = create_build_config(config_file)
    except BuildError as e:
        console.print(f"[red]Build config error: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(
        f"[blue]Publishing {package_config.name} v{package_config.version}"
        f"{' (dry-run)' if dry_run else ''}[/blue]"
    )

    # Build the tarball locally first. We deliberately re-run the build instead of
    # trusting whatever artifact might already be present at .matrix/package.tgz —
    # publish is too consequential to ship stale bytes.
    try:
        with create_progress_tracker(console) as progress:
            build_result = execute_build_workflow(progress, package_config)
    except BuildError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        logger.exception("Build failed")
        raise typer.Exit(1) from e

    tarball_path = build_result.package_file
    if not tarball_path.exists():
        console.print(
            f"[red]Expected tarball at {tarball_path} but it was not produced.[/red]"
        )
        raise typer.Exit(1)

    # Resolve publisher slug from the authenticated identity.
    client = _get_client()
    try:
        me = client.get_my_publisher()
    except (AuthenticationError, ResourceNotFoundError, ServiceError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    publisher_slug = me.get("slug")
    if not publisher_slug:
        console.print(
            "[red]No slug returned by /publishers/me — contact support.[/red]"
        )
        raise typer.Exit(1)

    if not me.get("publisherAgreementAcceptedAt"):
        console.print(
            "[red]Publisher Agreement not accepted. Visit /account/settings/publisher in"
            " your browser, accept the agreement, then retry.[/red]"
        )
        raise typer.Exit(1)

    console.print(
        f"[dim]Uploading {tarball_path.name} ({tarball_path.stat().st_size} bytes) to"
        f" {publisher_slug}/{package_config.name}...[/dim]"
    )

    try:
        response = client.publish(
            publisher_slug,
            package_config.name,
            tarball_path,
            dry_run=dry_run,
        )
    except ConflictError as e:
        # Must come before ValidationError — ConflictError is a subclass and
        # would otherwise be swallowed by the ValidationError branch.
        console.print(f"[red]Conflict: {e}[/red]")
        raise typer.Exit(1) from e
    except ValidationError as e:
        console.print(f"[red]Validation failed: {e}[/red]")
        # ValidationError instances carry the structured report on .report when
        # raised from the publish-client error mapper.
        _render_validation_report(getattr(e, "report", None))
        raise typer.Exit(2) from e
    except AuthenticationError as e:
        console.print(f"[red]Auth/agreement gate: {e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Publish failed: {e}[/red]")
        raise typer.Exit(1) from e

    if dry_run:
        console.print("[green]Dry-run OK — nothing published.[/green]")
        return

    if isinstance(response, dict):
        version = response.get("version") or package_config.version
        console.print(
            f"[green]Published {publisher_slug}/{package_config.name} v{version}[/green]"
        )
        if response.get("yankedAt"):
            console.print(
                f"[yellow]This version is currently yanked: {response.get('yankReason')}[/yellow]"
            )
    else:
        console.print("[green]Publish completed.[/green]")


# ----- yank / unyank -----


@app.command()
def yank(
    package_ref: str = typer.Argument(
        ..., help="Package reference in the form <name>@<version>."
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        "-r",
        help="Reason for yanking — shown to consumers of the yanked version.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Required when yanking the only remaining live version of a package. "
            "The server otherwise refuses with 409 — yanking the last live version "
            "effectively retires the package, which is almost never the intent."
        ),
    ),
) -> None:
    """Yank a published version so it disappears from default 'latest' resolution.

    Exact-version installs still resolve the yanked artifact (npm/PEP 592
    semantics) — downstream consumers pinned to the version aren't broken.
    """
    name, version = _parse_name_at_version(package_ref)
    client = _get_client()
    try:
        me = client.get_my_publisher()
        publisher_slug = me["slug"]
        result = client.yank(publisher_slug, name, version, reason, force=force)
    except (
        AuthenticationError,
        ConflictError,
        ResourceNotFoundError,
        ValidationError,
        ServiceError,
    ) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[yellow]Yanked {publisher_slug}/{name}@{version}[/yellow]")
    if isinstance(result, dict) and result.get("yankReason"):
        console.print(f"[dim]Reason: {result['yankReason']}[/dim]")


@app.command()
def unyank(
    package_ref: str = typer.Argument(
        ..., help="Package reference in the form <name>@<version>."
    ),
) -> None:
    """Unyank a previously-yanked version of your own package.

    Admin-yanked versions (yankedByAdmin=true) cannot be unyanked by the
    publisher — the server returns a 409 with an explanation.
    """
    name, version = _parse_name_at_version(package_ref)
    client = _get_client()
    try:
        me = client.get_my_publisher()
        publisher_slug = me["slug"]
        client.unyank(publisher_slug, name, version)
    except (
        AuthenticationError,
        ConflictError,
        ResourceNotFoundError,
        ValidationError,
        ServiceError,
    ) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]Unyanked {publisher_slug}/{name}@{version}[/green]")


# ----- list -----


@app.command(name="list")
def list_packages(
    page: int = typer.Option(
        1,
        "--page",
        help="1-indexed page of results to fetch. Default 1.",
        min=1,
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Results per page. Capped at 100 server-side regardless of input.",
        min=1,
        max=100,
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON to stdout instead of a human-readable table (machine-readable).",
    ),
    show_versions: bool = typer.Option(
        False,
        "--versions",
        help=(
            "Per-package version detail. Calls /packages/{publisher}/{name}/versions for each "
            "row and renders one line per version with its yank state and publish date."
        ),
    ),
) -> None:
    """List the packages you've published under your registry publisher.

    Default output is a compact one-line-per-package table. Use ``--versions`` to expand each
    row into its version history (yank state visible per version), or ``--json`` to emit the
    raw paged response for scripting. Use ``--page N`` to walk past the first page when your
    catalog exceeds ``--limit``.
    """
    client = _get_client()
    try:
        me = client.get_my_publisher()
        publisher_slug = me["slug"]
        # URL/UX is 1-indexed for the CLI flag; the API is 0-indexed.
        paged = client.list_my_packages(page=page - 1, size=limit)
    except (AuthenticationError, ResourceNotFoundError, ServiceError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    items = paged.get("items", []) if isinstance(paged, dict) else []
    total_results = (
        paged.get("totalResults", len(items)) if isinstance(paged, dict) else len(items)
    )
    total_pages = paged.get("totalPages", 1) if isinstance(paged, dict) else 1

    if json_output:
        # Machine-readable path stays clean — caller already gets the paginator data in the
        # envelope, so no human-only hints.
        import json as _json

        console.print_json(_json.dumps(paged))
        return

    if not items:
        if page > 1:
            console.print(
                f"[dim]No packages on page {page} (total: {total_results}). "
                f"Try `plgt list --page 1`.[/dim]"
            )
        else:
            console.print(
                "[dim]No packages published yet. "
                "Run `plgt publish` from a project directory to ship your first.[/dim]"
            )
        return

    from rich.table import Table

    if not show_versions:
        table = Table(
            title=f"Packages published by {publisher_slug}", title_justify="left"
        )
        table.add_column("Name", style="cyan")
        table.add_column("Latest", style="white")
        table.add_column("Versions", justify="right", style="white")
        table.add_column("Yanked", justify="right", style="yellow")
        table.add_column("Installs", justify="right", style="white")
        for pkg in items:
            yanked_count = pkg.get("yankedVersionCount", 0) or 0
            latest_yanked = pkg.get("latestVersionYanked", False)
            latest_v = pkg.get("latestVersion") or "—"
            latest_cell = f"{latest_v} (yanked)" if latest_yanked else latest_v
            table.add_row(
                f"{publisher_slug}/{pkg.get('name', '?')}",
                latest_cell,
                str(pkg.get("versionCount", 0) or 0),
                str(yanked_count) if yanked_count else "",
                str(pkg.get("installCount", 0) or 0),
            )
        console.print(table)
        # Render a precise "showing X-Y of N" + a `plgt list --page=N` hint when more pages
        # exist. Skipping the hint when there's only one page keeps the default output clean.
        start = (page - 1) * limit + 1
        end = start + len(items) - 1
        if total_pages > 1:
            console.print(
                f"[dim]Showing {start}-{end} of {total_results} packages "
                f"(page {page} of {total_pages}). "
                f"Run `plgt list --page {page + 1}` for the next page.[/dim]"
                if page < total_pages
                else (
                    f"[dim]Showing {start}-{end} of {total_results} packages "
                    f"(page {page} of {total_pages}; last page).[/dim]"
                )
            )
        else:
            console.print(
                f"[dim]{total_results} package(s) — "
                f"`plgt yank <name>@<version>` / `plgt unyank <name>@<version>` to manage[/dim]"
            )
        return

    # --versions: per-package expanded view. Fetch versions for each package via the public
    # versions endpoint. Loop is bounded by the per-publisher cap times the per-package version
    # cap so we're safe; if it ever gets slow we'll batch.
    from plgt.clients.registry_client import RegistryClient

    registry_client = RegistryClient(_get_client().session)
    for pkg in items:
        name = pkg.get("name", "?")
        console.print(f"\n[bold cyan]{publisher_slug}/{name}[/bold cyan]")
        try:
            versions = registry_client.list_compatible_versions(publisher_slug, name)
        except ServiceError as e:
            console.print(f"  [red]Failed to fetch versions: {e}[/red]")
            continue
        if not versions:
            console.print("  [dim]No versions yet.[/dim]")
            continue
        # registry_client returns lightweight refs (no yank state). Fall back to the package's
        # aggregate counts for "this version is yanked" badging.
        latest_version = pkg.get("latestVersion")
        latest_yanked = pkg.get("latestVersionYanked", False)
        for v in versions:
            ver = v.version
            badge = ""
            if ver == latest_version and latest_yanked:
                badge = " [yellow](yanked)[/yellow]"
            console.print(f"  v{ver}  [dim]engine: {v.engine_version}[/dim]{badge}")
