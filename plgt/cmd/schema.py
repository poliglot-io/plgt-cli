"""`plgt schema {describe|list|search}` commands — query the assembled graph.

All three commands consume the same assembled graph the validation
pipeline produces, so the schema query view is consistent with what
``plgt validate`` sees.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console

from plgt.core import settings
from plgt.services.diagnostics import Severity
from plgt.services.schema_service import (
    describe_term,
    list_terms,
    search_terms,
)
from plgt.services.validation_pipeline import validate_project
from plgt.utils.workspace_mode import resolve_workspace_mode

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()


app = typer.Typer(help="Query the assembled matrix schema.")


def _assemble_or_exit(
    project_dir: Path | None,
    *,
    workspace: str | None = None,
) -> object:
    """Run validate_project to produce the assembled graph. If the deps
    cache is missing, surface a clear error and exit non-zero.
    """
    target = (project_dir or Path.cwd()).resolve()
    result = validate_project(target, workspace=workspace)
    if result.assembled is None:
        for d in result.diagnostics.sorted():
            if d.severity == Severity.ERROR:
                console.print(f"[red]error: {d.code}: {d.message}[/red]")
        raise typer.Exit(1)
    return result.assembled


# Workspace-mode selection is shared with install / validate via
# plgt.utils.workspace_mode.resolve_workspace_mode.


@app.command("describe")
def schema_describe(
    uri: str = typer.Argument(
        ...,
        help="Full URI or prefixed name (e.g. `plgt-act:Action`) of the term to describe.",
    ),
    project_dir: Path | None = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Project directory. Defaults to current working directory.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty output."
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help="Query against the named workspace's cache subtree.",
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help="Query against the registry-resolve cache subtree.",
    ),
) -> None:
    """Dump structured metadata for a class, property, or named individual."""
    workspace = resolve_workspace_mode(
        from_workspace=from_workspace, from_registry=from_registry
    )
    graph = _assemble_or_exit(project_dir, workspace=workspace)
    description = describe_term(graph, uri)
    if description is None:
        console.print(f"[red]No term found in the assembled schema: {uri}[/red]")
        console.print(
            '[dim]Tip: try `plgt schema search "<keyword>"` to find similar '
            "terms, or `plgt schema list` to enumerate everything.[/dim]"
        )
        raise typer.Exit(1)

    if json_output:
        # JSON consumers expect full URIs (they're the stable identifier).
        typer.echo(json.dumps(description.to_dict()))
    else:
        # Human output is far more legible with prefixed names. Build a
        # URI → prefixed-name compactor from the assembled graph's namespace
        # bindings and apply it to every URI we surface.
        prefix_map = sorted(
            ((str(ns), prefix) for prefix, ns in graph.namespaces() if prefix),
            key=lambda pair: -len(pair[0]),  # longest-namespace wins on ties
        )
        _print_description(description, prefix_map)


def _compact(uri: str, prefix_map: list[tuple[str, str]]) -> str:
    """Render ``uri`` as ``prefix:localname`` if a bound prefix matches,
    otherwise return the URI unchanged.
    """
    for ns, prefix in prefix_map:
        if uri.startswith(ns):
            return f"{prefix}:{uri[len(ns) :]}"
    return uri


def _print_description(description, prefix_map: list[tuple[str, str]]) -> None:
    def _c(uri: str) -> str:
        return _compact(uri, prefix_map)

    def _cs(uris) -> str:
        return ", ".join(_c(u) for u in uris)

    console.print(f"[bold]{_c(description.uri)}[/bold]")
    if description.label:
        console.print(f"  label: {description.label}")
    if description.definition:
        console.print(f"  definition: {description.definition}")
    if description.comment:
        console.print(f"  comment: {description.comment}")
    if description.types:
        console.print(f"  type: {_cs(description.types)}")
    if description.subclass_of:
        console.print(f"  subClassOf: {_cs(description.subclass_of)}")
    if description.subclasses:
        console.print(f"  subclasses: {_cs(description.subclasses)}")
    if description.properties:
        console.print(f"  properties: {_cs(description.properties)}")
    if description.defined_in:
        console.print(f"  definedIn: {_c(description.defined_in)}")


@app.command("list")
def schema_list(
    type_filter: str | None = typer.Option(
        None,
        "--filter",
        help="Restrict to terms whose rdf:type equals this URI.",
    ),
    project_dir: Path | None = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Project directory. Defaults to current working directory.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty output."
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help="Query against the named workspace's cache subtree.",
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help="Query against the registry-resolve cache subtree.",
    ),
) -> None:
    """List every term in the assembled schema, optionally filtered by type."""
    workspace = resolve_workspace_mode(
        from_workspace=from_workspace, from_registry=from_registry
    )
    graph = _assemble_or_exit(project_dir, workspace=workspace)
    rows = list_terms(graph, type_filter=type_filter)

    if json_output:
        typer.echo(json.dumps(rows))
    else:
        for row in rows:
            label = f" — {row['label']}" if row["label"] else ""
            console.print(f"{row['uri']}{label}")


@app.command("search")
def schema_search(
    query: str = typer.Argument(
        ..., help="Substring to search for in labels, definitions, and comments."
    ),
    project_dir: Path | None = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Project directory. Defaults to current working directory.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of pretty output."
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help="Query against the named workspace's cache subtree.",
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help="Query against the registry-resolve cache subtree.",
    ),
) -> None:
    """Substring-search labels, definitions, and comments. Case-insensitive."""
    workspace = resolve_workspace_mode(
        from_workspace=from_workspace, from_registry=from_registry
    )
    graph = _assemble_or_exit(project_dir, workspace=workspace)
    hits = search_terms(graph, query)

    if json_output:
        typer.echo(json.dumps(hits))
    else:
        if not hits:
            console.print(f"[dim]No matches for {query!r}.[/dim]")
            return
        for hit in hits:
            label = f" — {hit['label']}" if hit["label"] else ""
            console.print(f"[bold]{hit['uri']}[/bold]{label}")
            console.print(f"  {hit['snippet']}")
