"""Structured diagnostics for the validation pipeline.

A diagnostic is a single finding from a validation phase: error or
warning, with stable code, file/line/column where known, message, and
optional did-you-mean suggestion. Stable codes (``PLGT_E####`` /
``PLGT_W####``) let agents pattern-match without parsing prose.

Output formats:

* terminal: pretty per-diagnostic block with colour and source context.
* JSON: one diagnostic per line (jsonl) for ``--json`` consumers.

Code ranges (initial allocation; expand within each band as needed):

* ``PLGT_E0001-0099`` — TTL parse errors
* ``PLGT_E0100-0199`` — SPARQL parse errors
* ``PLGT_E0200-0299`` — predicate / class existence
* ``PLGT_E0300-0399`` — namespace enforcement
* ``PLGT_E0400-0499`` — cross-reference integrity
* ``PLGT_E0500-0599`` — SHACL violations
* ``PLGT_E0600-0699`` — GREL function validation
* ``PLGT_E0700-0799`` — variable / secret resolution
* ``PLGT_E0800-0899`` — script:// resolution
* ``PLGT_E0900-0999`` — CLI invocation / precondition errors
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path


class Severity(str, Enum):
    """Diagnostic severity. ``error`` triggers a non-zero exit code."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Diagnostic:
    """Single validation finding.

    ``path`` is repo-relative when possible (the pipeline normalises against
    the project dir before emitting). ``line`` and ``col`` are 1-indexed.
    Unknown locations leave them as ``None``.

    ``subject`` is the URI of the RDF resource the diagnostic is about — the
    containing subject in TTL, or the SHACL focus node. It's the grouping key
    the CLI's pretty renderer uses to surface "all issues with `wgt-iss:Foo`"
    as a single section; per-phase emitters set it where applicable.

    ``defined_in`` is an optional file:line pointer used for did-you-mean
    suggestions ("Did you mean wgt-iss:hasState (defined in
    ontology.ttl:67)?") and for cross-reference diagnostics that want to
    point at the offending site's counterpart.
    """

    severity: Severity
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    col: int | None = None
    subject: str | None = None
    suggest: str | None = None
    defined_in: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            out["path"] = self.path
        if self.line is not None:
            out["line"] = self.line
        if self.col is not None:
            out["col"] = self.col
        if self.subject is not None:
            out["subject"] = self.subject
        if self.suggest is not None:
            out["suggest"] = self.suggest
        if self.defined_in is not None:
            out["defined_in"] = self.defined_in
        return out


@dataclass
class DiagnosticBag:
    """Accumulator for diagnostics produced across the pipeline.

    Mutable but never aliased between threads (the pipeline is
    single-threaded). The bag is the single source of truth for
    "did the build/validate pass" — checked at the end via ``has_errors()``.
    """

    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, diagnostic: Diagnostic) -> None:
        self.diagnostics.append(diagnostic)

    def error(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        line: int | None = None,
        col: int | None = None,
        subject: str | None = None,
        suggest: str | None = None,
        defined_in: str | None = None,
    ) -> None:
        self.add(
            Diagnostic(
                severity=Severity.ERROR,
                code=code,
                message=message,
                path=path,
                line=line,
                col=col,
                subject=subject,
                suggest=suggest,
                defined_in=defined_in,
            )
        )

    def warning(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        line: int | None = None,
        col: int | None = None,
        subject: str | None = None,
        suggest: str | None = None,
        defined_in: str | None = None,
    ) -> None:
        self.add(
            Diagnostic(
                severity=Severity.WARNING,
                code=code,
                message=message,
                path=path,
                line=line,
                col=col,
                subject=subject,
                suggest=suggest,
                defined_in=defined_in,
            )
        )

    def info(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        line: int | None = None,
        col: int | None = None,
        subject: str | None = None,
        suggest: str | None = None,
        defined_in: str | None = None,
    ) -> None:
        self.add(
            Diagnostic(
                severity=Severity.INFO,
                code=code,
                message=message,
                path=path,
                line=line,
                col=col,
                subject=subject,
                suggest=suggest,
                defined_in=defined_in,
            )
        )

    def has_errors(self) -> bool:
        return any(d.severity == Severity.ERROR for d in self.diagnostics)

    def sorted(self) -> list[Diagnostic]:
        """Stable order: errors before warnings before info; within a
        severity, by code, then by path (None last), then by line, col.

        Agents reading the first N lines of output should see the
        highest-leverage findings.
        """
        severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

        def key(d: Diagnostic) -> tuple:
            return (
                severity_order[d.severity],
                d.code,
                d.path or "￿",  # None sorts last
                d.line or 0,
                d.col or 0,
            )

        return sorted(self.diagnostics, key=key)


def diagnostics_as_jsonl(diagnostics: Iterable[Diagnostic]) -> str:
    """Render as JSON Lines (one diagnostic per line). Stable, sorted output
    is the caller's responsibility — call ``bag.sorted()`` first.
    """
    return "\n".join(
        json.dumps(d.to_dict(), separators=(",", ":")) for d in diagnostics
    )


def diagnostics_as_text(diagnostics: Iterable[Diagnostic]) -> str:
    """Render as human-readable terminal output, one diagnostic per block.

    No colour codes here — that's the caller's responsibility (uses ``rich``
    in the CLI). This is the plain-text representation for log files and
    non-TTY consumers that still want prose.
    """
    lines: list[str] = []
    for d in diagnostics:
        location = ""
        if d.path:
            location = d.path
            if d.line:
                location += f":{d.line}"
                if d.col:
                    location += f":{d.col}"
            location += ": "
        lines.append(f"{d.severity.value}: {d.code}: {location}{d.message}")
        if d.suggest:
            lines.append(f"  did you mean: {d.suggest}")
        if d.defined_in:
            lines.append(f"  defined in: {d.defined_in}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grouped diagnostics — the layout the CLI's pretty renderer uses.
#
# Each ``ResourceGroup`` is one ``(path, subject)`` bucket: all diagnostics
# about a single RDF resource in a single file end up in one group. Authors
# read one group at a time when they open the file, so the grouping key is
# also the truncation unit ("show me the first N noisy resources, dump the
# rest to a file"). A diagnostic with no ``path`` sits in a "graph-level"
# bucket; one with a path but no ``subject`` sits in a "file-level"
# pseudo-subject within its file.
# ---------------------------------------------------------------------------


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class ResourceGroup:
    """All findings about a single ``(path, subject)`` pair, in line order.

    ``path is None`` means graph-level (no file anchor — typically a
    project-level precondition error or a SHACL finding the validator can't
    pin to a source file). ``subject is None`` means file-level (a TTL/SPARQL
    parse error, or any other diagnostic the phase didn't tag with a
    containing RDF subject).
    """

    path: str | None
    subject: str | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def worst_severity(self) -> Severity:
        """Highest-priority severity in the group. Used as the primary sort
        key so a group with even one error outranks a group of warnings.
        """
        return min(
            (d.severity for d in self.diagnostics),
            key=lambda s: _SEVERITY_RANK[s],
            default=Severity.INFO,
        )


@dataclass(frozen=True)
class GroupedDiagnostics:
    """Ordered set of ``ResourceGroup``s plus totals for summary rendering.

    The totals are precomputed because the CLI prints them in the footer
    regardless of truncation, and recomputing them from a truncated view
    would be wrong.
    """

    groups: tuple[ResourceGroup, ...]
    total_findings: int

    @property
    def total_resources(self) -> int:
        return len(self.groups)

    def split(self, limit: int) -> tuple[GroupedDiagnostics, GroupedDiagnostics]:
        """Return ``(shown, omitted)`` split at ``limit`` groups. Totals are
        recomputed for each half. ``limit <= 0`` returns ``(empty, self)``;
        a limit at or above the group count returns ``(self, empty)``.
        """
        if limit <= 0:
            return _empty_grouped(), self
        if limit >= len(self.groups):
            return self, _empty_grouped()
        shown_groups = self.groups[:limit]
        omitted_groups = self.groups[limit:]
        shown = GroupedDiagnostics(
            groups=shown_groups,
            total_findings=sum(len(g.diagnostics) for g in shown_groups),
        )
        omitted = GroupedDiagnostics(
            groups=omitted_groups,
            total_findings=sum(len(g.diagnostics) for g in omitted_groups),
        )
        return shown, omitted


def _empty_grouped() -> GroupedDiagnostics:
    return GroupedDiagnostics(groups=(), total_findings=0)


def group_diagnostics(diagnostics: Iterable[Diagnostic]) -> GroupedDiagnostics:
    """Bucket ``diagnostics`` by ``(path, subject)`` and sort for display.

    Group ordering: files cluster together, sorted by the worst severity
    *in that file* — so a file with even one error outranks a file of pure
    warnings, but a single file's warning-only resources never get
    interleaved between error-bearing resources of another file. Within a
    file, subjects sort by their own worst severity, then by URI. The
    `￿` sentinel keeps graph-level / file-level pseudo-buckets at the
    bottom of their tier.

    Within a group, diagnostics order by ``(severity, line-or-0, col-or-0,
    code)`` so errors precede warnings precede info, and within a severity
    the reader walks the file top-down.
    """
    buckets: dict[tuple[str | None, str | None], list[Diagnostic]] = {}
    for d in diagnostics:
        buckets.setdefault((d.path, d.subject), []).append(d)

    def diag_key(d: Diagnostic) -> tuple:
        return (
            _SEVERITY_RANK[d.severity],
            d.line or 0,
            d.col or 0,
            d.code,
        )

    groups: list[ResourceGroup] = []
    for (path, subject), items in buckets.items():
        groups.append(
            ResourceGroup(
                path=path,
                subject=subject,
                diagnostics=tuple(sorted(items, key=diag_key)),
            )
        )

    # Per-file worst-severity rank — used so a file's warning-only subjects
    # don't get interleaved between error groups of other files. Computed
    # once, looked up per group.
    worst_by_path: dict[str | None, int] = {}
    for g in groups:
        rank = _SEVERITY_RANK[g.worst_severity]
        prior = worst_by_path.get(g.path)
        if prior is None or rank < prior:
            worst_by_path[g.path] = rank

    def group_key(g: ResourceGroup) -> tuple:
        return (
            worst_by_path[g.path],  # file tier — cluster first
            g.path or "￿",  # alphabetical within the tier
            _SEVERITY_RANK[g.worst_severity],  # subject severity within the file
            g.subject or "￿",
        )

    groups.sort(key=group_key)
    return GroupedDiagnostics(
        groups=tuple(groups),
        total_findings=sum(len(g.diagnostics) for g in groups),
    )


def _default_format_subject(subject: str | None) -> str:
    """Fallback subject formatter: full URI in angle brackets, or the
    placeholder for groups with no RDF anchor.
    """
    return f"<{subject}>" if subject else "(no subject)"


def render_diagnostics_grouped(
    grouped: GroupedDiagnostics,
    *,
    format_subject: Callable[[str | None], str] | None = None,
) -> str:
    """Plain-text grouped renderer. No colour codes; the CLI's terminal
    output uses a separate ``rich``-aware renderer that walks the same
    ``GroupedDiagnostics`` structure. This is what the overflow report file
    contains, and what non-TTY consumers see.

    ``format_subject`` abbreviates a subject URI for display (e.g.,
    ``iam:AdminPolicy`` when a prefix is bound; ``<...>`` otherwise). The
    CLI builds one backed by the assembled graph's namespace manager so the
    rendered form matches what an author wrote in TTL. Defaults to wrapping
    the URI in angle brackets so this module stays rdflib-free.

    Format (file header prints once per file, subjects nest underneath):

        path/to/file.ttl
          iam:AdminPolicy
            error    PLGT_E0500  L23   message
                                       did you mean: ...
            warning  PLGT_W0500        message
          iam:UserPolicy
            error    PLGT_E0501  L42   ...

        path/to/other.ttl
          ...
    """
    fmt_subject = format_subject or _default_format_subject
    lines: list[str] = []
    _missing = object()  # sentinel so None (graph-level) is a real path value
    last_path: object = _missing
    for group in grouped.groups:
        if group.path != last_path:
            if last_path is not _missing:
                lines.append("")  # blank line between files
            lines.append(group.path if group.path else "(graph-level findings)")
            last_path = group.path
        lines.append(f"  {fmt_subject(group.subject)}")
        for d in group.diagnostics:
            location = ""
            if d.line:
                location = f"L{d.line}"
                if d.col:
                    location += f":{d.col}"
            # Columns: severity (8) + code (11) + location (8) + message.
            # Widths chosen so "warning" + "PLGT_E0500" + "L9999" lines up.
            lines.append(
                f"    {d.severity.value:<8}{d.code:<11} {location:<8} {d.message}"
            )
            if d.suggest:
                lines.append(f"{'':<32}did you mean: {d.suggest}")
            if d.defined_in:
                lines.append(f"{'':<32}defined in: {d.defined_in}")
    return "\n".join(lines)


def relative_path(path: Path, project_dir: Path) -> str:
    """Render ``path`` relative to ``project_dir`` if it's underneath; else
    fall back to ``str(path)``. Diagnostic ``path`` fields always carry
    repo-relative paths when possible to keep terminal output compact.
    """
    try:
        return str(path.resolve().relative_to(project_dir.resolve()))
    except ValueError:
        return str(path)
