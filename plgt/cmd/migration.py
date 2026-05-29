"""Migration authoring commands for the Poliglot CLI.

Two subcommands:

* ``plgt migration new --from <version>`` — scaffold a ``from-<version>.rq`` file.
* ``plgt migration diff --from <version> --to <version>`` — structural diff between two versions
  of the matrix definition; prints suggested migration scaffolding (heuristic, not a generator).

Migration test execution lives server-side. The CLI deliberately ships no test runner:
rdflib's SPARQL processor diverges from the authoritative SPARQL engine on extensions
(property functions, action invocations, custom built-ins), so a CLI-side golden-file
runner would give false confidence.
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003 — Typer reads annotations at runtime

import typer
from rdflib import Graph
from rich.console import Console

from plgt.core import settings
from plgt.services.build_service import create_build_config

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()

app = typer.Typer(help="Author matrix migrations.")


_SCAFFOLD_TEMPLATE = """\
# Migration: from {from_version} to next
#
# Author the SPARQL UPDATE that rewrites graph data shaped for {from_version} into the shape
# expected by the next published version of this matrix. Most evolution should use additive
# vocabulary instead of a migration.

DELETE {{
  # ?s old:predicate ?o .
}}
INSERT {{
  # ?s new:predicate ?o .
}}
WHERE {{
  # ?s old:predicate ?o .
}}
"""


def _resolve_matrix_dir(matrix: str | None) -> Path:
    """Find the target matrix directory from poliglot.yml.

    With one matrix, the choice is unambiguous; with multiple, ``--matrix`` is required.
    """
    config = create_build_config(None)
    if not config.matrices:
        msg = "No matrices declared in poliglot.yml"
        raise typer.BadParameter(msg)
    if matrix is None:
        if len(config.matrices) > 1:
            names = ", ".join(m.name for m in config.matrices)
            msg = f"Multiple matrices declared ({names}); pass --matrix <name>"
            raise typer.BadParameter(msg)
        matrix_config = config.matrices[0]
    else:
        candidates = [m for m in config.matrices if m.name == matrix]
        if not candidates:
            msg = f"No matrix named '{matrix}' in poliglot.yml"
            raise typer.BadParameter(msg)
        matrix_config = candidates[0]

    return config.project_dir / matrix_config.path


@app.command(name="new")
def new(
    from_version: str = typer.Option(
        ...,
        "--from",
        help="Source version that the migration transforms data away from.",
    ),
    matrix: str | None = typer.Option(
        None,
        "--matrix",
        help="Matrix name (required when poliglot.yml declares more than one).",
    ),
):
    """Scaffold a new migration file."""
    matrix_dir = _resolve_matrix_dir(matrix)
    migrations_dir = matrix_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)

    rq_path = migrations_dir / f"from-{from_version}.rq"

    if rq_path.exists():
        msg = f"Migration already exists: {rq_path.relative_to(matrix_dir)}"
        raise typer.BadParameter(msg)
    # Force UTF-8 — TTL/SPARQL fixtures routinely include non-ASCII IRIs and the platform
    # default encoding (cp1252 on Windows) would silently mangle them.
    rq_path.write_text(
        _SCAFFOLD_TEMPLATE.format(from_version=from_version), encoding="utf-8"
    )

    console.print(
        f"[green]Created migration: {rq_path.relative_to(matrix_dir)}[/green]"
    )


@app.command(name="diff")
def diff(
    from_path: Path = typer.Option(
        ..., "--from", help="Path to the older matrix definition."
    ),
    to_path: Path = typer.Option(
        ..., "--to", help="Path to the newer matrix definition."
    ),
):
    """Structural diff between two matrix definition graphs.

    Reports predicates and classes added, removed, or moved between versions. The output is a
    starting point for authoring a migration — it is a *heuristic*, not a generator. The author
    still writes the SPARQL UPDATE; this command just narrows the search.
    """
    if not from_path.exists():
        msg = f"--from path does not exist: {from_path}"
        raise typer.BadParameter(msg)
    if not to_path.exists():
        msg = f"--to path does not exist: {to_path}"
        raise typer.BadParameter(msg)
    from_graph = _load_graph(from_path)
    to_graph = _load_graph(to_path)

    from_predicates = {str(p) for p in from_graph.predicates()}
    to_predicates = {str(p) for p in to_graph.predicates()}
    removed_predicates = sorted(from_predicates - to_predicates)
    added_predicates = sorted(to_predicates - from_predicates)

    from_classes = _classes_in(from_graph)
    to_classes = _classes_in(to_graph)
    removed_classes = sorted(from_classes - to_classes)
    added_classes = sorted(to_classes - from_classes)

    if not (removed_predicates or added_predicates or removed_classes or added_classes):
        console.print("[green]No structural differences detected.[/green]")
        return

    if removed_predicates:
        console.print("[yellow]Removed predicates (likely needs a migration):[/yellow]")
        for uri in removed_predicates:
            console.print(f"  - {uri}")
    if added_predicates:
        console.print("[blue]Added predicates (additive — no migration needed):[/blue]")
        for uri in added_predicates:
            console.print(f"  + {uri}")
    if removed_classes:
        console.print("[yellow]Removed classes (likely needs a migration):[/yellow]")
        for uri in removed_classes:
            console.print(f"  - {uri}")
    if added_classes:
        console.print("[blue]Added classes (additive — no migration needed):[/blue]")
        for uri in added_classes:
            console.print(f"  + {uri}")

    if removed_predicates or removed_classes:
        console.print(
            "\n[bold]Suggested next step:[/bold] author a migration that rewrites instance data"
            " referencing the removed terms into the new shape."
        )


def _load_graph(path: Path) -> Graph:
    """Parse one or many TTL files into a single graph."""
    graph = Graph()
    if path.is_file():
        graph.parse(path, format="turtle")
    elif path.is_dir():
        for ttl in path.rglob("*.ttl"):
            graph.parse(ttl, format="turtle")
    return graph


def _classes_in(graph: Graph) -> set[str]:
    """Collect class URIs declared via ``rdf:type rdfs:Class`` or ``owl:Class``.

    Blank-node classes are skipped — comparison only makes sense for stable URIs.
    """
    from rdflib import URIRef
    from rdflib.namespace import OWL, RDF, RDFS

    classes: set[str] = set()
    for cls in graph.subjects(RDF.type, RDFS.Class):
        if isinstance(cls, URIRef):
            classes.add(str(cls))
    for cls in graph.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef):
            classes.add(str(cls))
    return classes
