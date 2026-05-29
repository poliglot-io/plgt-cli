"""`plgt validate` command — local validation pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rdflib import URIRef
from rich.console import Console
from rich.text import Text

from plgt.core import settings
from plgt.services.diagnostics import (
    GroupedDiagnostics,
    ResourceGroup,
    Severity,
    diagnostics_as_jsonl,
    group_diagnostics,
    render_diagnostics_grouped,
)
from plgt.services.validation_pipeline import ValidationResult, validate_project
from plgt.utils.workspace_mode import resolve_workspace_mode

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()


_SEVERITY_STYLE = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


_DEFAULT_MAX_RESOURCES = 10
_OVERFLOW_REPORT_PATH = Path(".matrix") / "reports" / "validate.txt"


app = typer.Typer(help="Validate a matrix project locally.")


@app.callback(invoke_without_command=True)
def validate(
    ctx: typer.Context,
    project_dir: Path | None = typer.Option(
        None,
        "--project-dir",
        "-d",
        help="Project directory. Defaults to current working directory.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit one JSON diagnostic per line for machine consumption.",
    ),
    max_resources: int = typer.Option(
        _DEFAULT_MAX_RESOURCES,
        "--max-resources",
        help=(
            "Maximum number of resources ((file, subject) buckets) to print to "
            "the terminal. Remaining resources overflow to "
            f"./{_OVERFLOW_REPORT_PATH}. Use 0 to print all."
        ),
    ),
    from_workspace: str | None = typer.Option(
        None,
        "--from-workspace",
        help=(
            "Validate against the per-workspace deps cache populated by "
            "`plgt sync --from-workspace <slug>`. Mutually exclusive with "
            "--from-registry. Defaults to the configured default workspace "
            "if neither flag is passed."
        ),
    ),
    from_registry: bool = typer.Option(
        False,
        "--from-registry",
        help=(
            "Validate against the registry-resolve deps cache populated by "
            "`plgt sync --from-registry`. Mutually exclusive with "
            "--from-workspace."
        ),
    ),
) -> None:
    """Run the local validation pipeline.

    Exits with:
    * ``0`` — clean (no errors; warnings may be present)
    * ``1`` — one or more errors
    * ``2`` — invocation error (missing files, malformed args)
    """
    if ctx.invoked_subcommand is not None:
        return

    workspace = resolve_workspace_mode(
        from_workspace=from_workspace, from_registry=from_registry
    )

    target = (project_dir or Path.cwd()).resolve()
    result = validate_project(target, workspace=workspace)
    sorted_diagnostics = result.diagnostics.sorted()

    if json_output:
        # `--json` is the machine-consumption contract: full stream, one
        # diagnostic per line, no truncation, no overflow file. Untouched.
        if sorted_diagnostics:
            typer.echo(diagnostics_as_jsonl(sorted_diagnostics))
    elif sorted_diagnostics:
        grouped = group_diagnostics(sorted_diagnostics)
        # max_resources=0 means "print everything"; treat as no limit.
        limit = max_resources if max_resources > 0 else len(grouped.groups)
        shown, omitted = grouped.split(limit)
        format_subject = _make_subject_formatter(result)
        _print_grouped(shown, format_subject=format_subject)
        if omitted.groups:
            overflow_path = _write_overflow_report(
                target, grouped, format_subject=format_subject
            )
            _print_truncation_footer(omitted, overflow_path)
        _print_summary_footer(grouped)
    else:
        console.print("[green]Validation clean.[/green]")

    error_count = sum(1 for d in sorted_diagnostics if d.severity == Severity.ERROR)
    if error_count:
        raise typer.Exit(1)


def _make_subject_formatter(
    result: ValidationResult,
) -> Callable[[str | None], str]:
    """Build a subject formatter backed by the assembled graph's namespace
    table. With a graph in hand we can render ``iam:AdminPolicy`` instead
    of ``<https://example.com/iam#AdminPolicy>``; without one (the assembly
    failed earlier in the pipeline), we fall back to angle-bracket form.

    rdflib's ``compute_qname(generate=False)`` raises when no prefix is
    bound for the URI's namespace — we catch and fall back rather than
    letting it invent ``ns1:`` style prefixes the author never wrote.
    """
    graph = result.assembled
    if graph is None:
        return _default_subject_format

    def fmt(subject: str | None) -> str:
        if subject is None:
            return "(no subject)"
        try:
            prefix, _ns, local = graph.compute_qname(URIRef(subject), generate=False)
        except Exception:  # noqa: BLE001 — rdflib raises Error/KeyError; fall back
            return f"<{subject}>"
        return f"{prefix}:{local}" if prefix else f"<{subject}>"

    return fmt


def _default_subject_format(subject: str | None) -> str:
    return f"<{subject}>" if subject else "(no subject)"


def _print_grouped(
    grouped: GroupedDiagnostics,
    *,
    format_subject: Callable[[str | None], str],
) -> None:
    """Render the styled per-resource view. The file header prints once
    per file and subjects nest under it; consecutive groups for the same
    path share that header so the reader sees "this file has these N
    resources" instead of "this file" repeated N times.
    """
    _missing = object()  # sentinel so None (graph-level) is a real value
    last_path: object = _missing
    for group in grouped.groups:
        if group.path != last_path:
            if last_path is not _missing:
                console.print()
            _print_file_header(group.path)
            last_path = group.path
        _print_subject_section(group, format_subject)


def _print_file_header(path: str | None) -> None:
    header = Text()
    if path:
        header.append(path, style="bold underline")
    else:
        header.append("(graph-level findings)", style="bold dim")
    console.print(header)


def _print_subject_section(
    group: ResourceGroup, format_subject: Callable[[str | None], str]
) -> None:
    subject_line = Text("  ")
    rendered = format_subject(group.subject)
    if group.subject:
        subject_line.append(rendered, style="cyan")
    else:
        subject_line.append(rendered, style="dim italic")
    console.print(subject_line)

    for d in group.diagnostics:
        line = Text("    ")
        line.append(
            f"{d.severity.value:<8}",
            style=_SEVERITY_STYLE.get(d.severity, ""),
        )
        line.append(f"{d.code:<11}", style="dim")
        location = ""
        if d.line:
            location = f"L{d.line}"
            if d.col:
                location += f":{d.col}"
        line.append(f"{location:<8} ", style="dim")
        line.append(d.message)
        console.print(line)
        if d.suggest:
            console.print(Text(f"{'':<32}did you mean: {d.suggest}", style="green"))
        if d.defined_in:
            console.print(Text(f"{'':<32}defined in: {d.defined_in}", style="dim"))


def _write_overflow_report(
    project_dir: Path,
    grouped: GroupedDiagnostics,
    *,
    format_subject: Callable[[str | None], str],
) -> Path:
    """Dump the full uncolored grouped report. Overwrites each run — the
    file is "the current validate output", not a history. Returns the path
    written so the truncation footer can point at it.
    """
    overflow_path = project_dir / _OVERFLOW_REPORT_PATH
    overflow_path.parent.mkdir(parents=True, exist_ok=True)
    overflow_path.write_text(
        render_diagnostics_grouped(grouped, format_subject=format_subject),
        encoding="utf-8",
    )
    return overflow_path


def _print_truncation_footer(omitted: GroupedDiagnostics, overflow_path: Path) -> None:
    console.print()
    console.print(
        Text.assemble(
            (
                f"… {omitted.total_resources} more resource(s) with "
                f"{omitted.total_findings} finding(s)",
                "dim",
            ),
            (f"\nFull report: {overflow_path}", "dim italic"),
        )
    )


def _print_summary_footer(grouped: GroupedDiagnostics) -> None:
    counts = {Severity.ERROR: 0, Severity.WARNING: 0, Severity.INFO: 0}
    for g in grouped.groups:
        for d in g.diagnostics:
            counts[d.severity] += 1
    parts = []
    if counts[Severity.ERROR]:
        parts.append(("red bold", f"{counts[Severity.ERROR]} error(s)"))
    if counts[Severity.WARNING]:
        parts.append(("yellow", f"{counts[Severity.WARNING]} warning(s)"))
    if counts[Severity.INFO]:
        parts.append(("cyan", f"{counts[Severity.INFO]} info"))
    summary = Text("\nSummary: ", style="bold")
    for i, (style, label) in enumerate(parts):
        if i > 0:
            summary.append(", ")
        summary.append(label, style=style)
    summary.append(f" across {grouped.total_resources} resource(s).", style="dim")
    console.print(summary)
