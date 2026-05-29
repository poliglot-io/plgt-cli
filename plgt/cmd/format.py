"""``plgt format`` — format Turtle and SPARQL files in place.

Dispatches by extension: ``.ttl`` through the Turtle formatter, ``.rq``
(and ``.sparql``) through the SPARQL formatter. Walks directories
recursively, skipping vendored/build/dependency trees.

Exit codes:

* ``0`` — clean (or, with ``--check``, no files would change).
* ``1`` — with ``--check``, at least one file would change. Without
  ``--check``, this exit code is reserved for hard errors.
* ``2`` — usage error (no paths, unknown extension on a single file).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from plgt.services.formatter import format_sparql, format_turtle

app = typer.Typer(help="Format Turtle and SPARQL files in place.")
console = Console()


# Directory names that are never descended into. These are
# vendored/build/cache trees that don't belong in the format set.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".matrix",
        ".m2",
        ".tmp",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "target",
        "__pycache__",
    }
)

# Path segments that signal a deliberate, formatter-hostile fixture tree.
# Test fixtures often encode invariants the formatter doesn't preserve
# (specific blank-node layout, comment placement that drives parser
# behaviour, deliberate prefixed-name forms). We skip the whole
# `src/test/resources/` subtree across all modules — anyone who wants a
# fixture reformatted should do it explicitly.
_SKIP_PATH_SEGMENTS = (
    "src/test/resources",
    "src/main/resources/spec",
)


def _should_skip(child: Path) -> bool:
    parts = child.parts
    if any(p in _SKIP_DIRS for p in parts):
        return True
    # Path-segment match — joined check so the segments above match
    # across slash-separated boundaries.
    posix = "/".join(parts)
    return any(seg in posix for seg in _SKIP_PATH_SEGMENTS)


def _formatter_for(path: Path) -> callable | None:
    """Return the formatter to use for ``path``, or None if the file's
    extension isn't one we handle."""
    suffix = path.suffix.lower()
    if suffix == ".ttl":
        return format_turtle
    if suffix in {".rq", ".sparql"}:
        return format_sparql
    return None


def _walk_paths(paths: list[Path]) -> list[Path]:
    """Resolve ``paths`` to a flat list of files to format. Directories
    are walked recursively; files are kept as-is; unknown extensions are
    silently dropped (but the caller surfaces a usage error when no
    formattable files were found, so the user isn't left wondering)."""
    out: list[Path] = []
    for p in paths:
        if p.is_file():
            if _formatter_for(p) is None:
                continue
            # Skip-set applies to explicit file paths too — pre-commit
            # passes staged filenames directly, and we don't want a
            # `git add path/to/test/resources/foo.ttl`
            # to bypass the fixture skip just because it wasn't reached
            # via directory walking.
            if _should_skip(p):
                continue
            out.append(p)
        elif p.is_dir():
            for child in p.rglob("*"):
                if not child.is_file():
                    continue
                if _should_skip(child):
                    continue
                if _formatter_for(child) is not None:
                    out.append(child)
    return out


@app.callback(invoke_without_command=True)
def format_cmd(
    ctx: typer.Context,
    paths: list[Path] = typer.Argument(
        None,
        help=(
            "Files or directories to format. Directories are walked "
            "recursively. With no argument, formats the current "
            "directory."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Don't write anything — exit 1 if any file would change. "
            "Mirrors the ergonomics of `ruff format --check` / "
            "`prettier --check` for CI use."
        ),
    ),
    diff: bool = typer.Option(
        False,
        "--diff",
        help=(
            "Print a unified diff of every file that would change "
            "without writing. Implies --check (no writes)."
        ),
    ),
) -> None:
    """Format Turtle (`.ttl`) and SPARQL (`.rq`, `.sparql`) files.

    The formatter is opinionated and has no configuration — same input
    always produces the same output. Both the CLI and the LSP route to
    this same module, so the editor and command line agree on canonical
    output.
    """
    if ctx.invoked_subcommand is not None:
        return

    if not paths:
        paths = [Path.cwd()]

    targets = _walk_paths([p.resolve() for p in paths])
    if not targets:
        # No formattable files at the given path(s). When the caller
        # passed explicit files (pre-commit's mode), this is the common
        # "all inputs were skipped" case and not an error — exit 0.
        # When the caller passed a directory, surface as exit 2 so a
        # confused user gets a clearer signal.
        all_explicit_files = all(p.is_file() for p in paths)
        if all_explicit_files:
            return
        console.print(
            "[yellow]no .ttl or .rq files found at the given path(s).[/yellow]"
        )
        raise typer.Exit(code=2)

    would_change: list[Path] = []
    changed: list[Path] = []
    errored: list[tuple[Path, str]] = []

    for target in targets:
        try:
            original = target.read_text(encoding="utf-8")
            fmt = _formatter_for(target)
            assert fmt is not None  # _walk_paths filtered
            formatted = fmt(original)
        except OSError as e:
            errored.append((target, f"read error: {e}"))
            continue
        except Exception as e:  # noqa: BLE001 — surface but don't abort
            errored.append((target, f"formatter error: {e}"))
            continue

        if formatted == original:
            continue

        if diff:
            _print_diff(target, original, formatted)
        if check or diff:
            would_change.append(target)
            continue
        try:
            target.write_text(formatted, encoding="utf-8")
        except OSError as e:
            errored.append((target, f"write error: {e}"))
            continue
        changed.append(target)

    # Output summary.
    if errored:
        for path, msg in errored:
            console.print(f"[red]error[/red] {path}: {msg}", highlight=False)

    if check or diff:
        if would_change:
            console.print(
                f"[yellow]{len(would_change)} file(s) would be reformatted.[/yellow]"
            )
            for p in would_change:
                console.print(f"  {p}")
            raise typer.Exit(code=1)
        console.print(f"[green]all {len(targets)} file(s) already formatted.[/green]")
        if errored:
            raise typer.Exit(code=1)
        return

    if changed:
        console.print(f"[green]reformatted {len(changed)} file(s).[/green]")
    else:
        console.print(f"[green]all {len(targets)} file(s) already formatted.[/green]")
    if errored:
        raise typer.Exit(code=1)


def _print_diff(path: Path, original: str, formatted: str) -> None:
    """Render a unified diff to stdout. We use difflib directly rather
    than shelling to `diff` so the output is consistent on every OS and
    so tests can capture it without subprocess plumbing."""
    from difflib import unified_diff

    diff_lines = list(
        unified_diff(
            original.splitlines(keepends=True),
            formatted.splitlines(keepends=True),
            fromfile=f"{path} (original)",
            tofile=f"{path} (formatted)",
        )
    )
    sys.stdout.writelines(diff_lines)
