"""`plgt lsp` — launch the Language Server on stdio.

Run as a subprocess from an editor:

* VS Code: configure ``plgt lsp`` as the command for the
  ``poliglot.plgt`` extension (separate package).
* Other editors: any LSP client that can launch a subprocess works.

The server reads JSON-RPC over stdin and writes responses over stdout.
Stderr is reserved for diagnostic logs.

Structured as a plain command function (registered on the root app via
``app.command(name="lsp")(lsp_command)``) rather than a ``typer.Typer``
group; ``plgt lsp --help`` then renders as ``Usage: plgt lsp [OPTIONS]``
instead of ``[OPTIONS] COMMAND [ARGS]...`` (which would suggest there
are subcommands when there aren't).
"""

from __future__ import annotations

import typer

from plgt.services.lsp_server import run_stdio


def lsp_command(
    stdio: bool = typer.Option(
        False,
        "--stdio",
        help="Use stdio transport (the default and only supported transport). "
        "Accepted for compatibility with LSP clients (e.g. vscode-languageclient) "
        "that auto-append `--stdio` when configured for stdio transport.",
    ),
) -> None:
    """Launch the Language Server Protocol server on stdio.

    Blocks until the editor client disconnects. Stdout carries the LSP
    wire protocol; stderr carries diagnostic logs.
    """
    run_stdio()
