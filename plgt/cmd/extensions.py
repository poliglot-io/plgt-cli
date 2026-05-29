"""Extension commands for Poliglot CLI.

This module implements the CLI commands for managing matrix extensions.
Extensions are merged into matrices at assembly time when CRUD operations
trigger partial re-deployment. All operations require workspace admin permissions.
"""

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from plgt.clients.extension_client import ExtensionClient
from plgt.core import config
from plgt.core.exceptions import ServiceError

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(help="Matrix extension operations.")


def _get_workspace(workspace: str | None) -> str:
    """Resolve workspace from argument or default config."""
    if workspace:
        return workspace
    default = config.defaults.get("workspace")
    if not default:
        console.print(
            "[red]No workspace specified. Use --workspace or run 'plgt auth sync' to configure a default.[/red]"
        )
        raise typer.Exit(1)
    return default


def _get_client() -> ExtensionClient:
    """Get authenticated extension client."""
    session = config.get_session()
    if not session.authenticated:
        console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
        raise typer.Exit(1)
    return ExtensionClient(session)


@app.command()
def create(
    matrix: str = typer.Option(
        ...,
        "--matrix",
        "-m",
        help="Target matrix URI to extend.",
    ),
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Path to Turtle content file.",
        exists=True,
        readable=True,
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        "-l",
        help="Optional label for the extension.",
    ),
):
    """Create a new matrix extension.

    Creates the extension and triggers partial re-deployment of the target matrix.
    The extension will be marked as 'pending' until the assembly completes.
    Requires workspace admin permissions.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        console.print(f"[dim]Creating extension for matrix '{matrix}'...[/dim]")

        extension = client.create_extension(
            target_workspace,
            matrix,
            file,
            label=label,
        )

        status = (
            "[green]active[/green]" if extension.active else "[yellow]pending[/yellow]"
        )

        console.print("[green]Extension created successfully![/green]")
        console.print(f"  ID: [bold]{extension.id}[/bold]")
        console.print(f"  Label: {extension.label}")
        console.print(f"  Matrix: {extension.target_matrix_uri}")
        console.print(f"  Status: {status}")

    except ServiceError as e:
        console.print(f"[red]Failed to create extension: {e}[/red]")
        logger.exception("Failed to create extension")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error creating extension")
        raise typer.Exit(1) from e


@app.command(name="list")
def list_extensions(
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
):
    """List all matrix extensions in a workspace.

    Requires workspace admin permissions.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        extensions = client.list_extensions(target_workspace)

        if not extensions:
            console.print("[yellow]No extensions found.[/yellow]")
            raise typer.Exit(0)

        table = Table(title=f"Extensions in '{target_workspace}'", show_lines=True)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Label")
        table.add_column("Matrix", max_width=40)
        table.add_column("Status")
        table.add_column("Owner")
        table.add_column("Updated")

        for ext in extensions:
            status = (
                "[green]active[/green]" if ext.active else "[yellow]pending[/yellow]"
            )
            table.add_row(
                str(ext.id),
                ext.label,
                ext.target_matrix_uri,
                status,
                ext.owner_username,
                ext.updated_at.strftime("%Y-%m-%d %H:%M"),
            )

        console.print(table)

    except ServiceError as e:
        console.print(f"[red]Failed to list extensions: {e}[/red]")
        logger.exception("Failed to list extensions")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error listing extensions")
        raise typer.Exit(1) from e


@app.command()
def get(
    extension_id: str = typer.Argument(..., help="The extension ID."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path for the Turtle content.",
    ),
):
    """Get a single extension by ID.

    If --output is specified, writes the Turtle content to the file.
    Otherwise, displays extension details and content.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        extension = client.get_extension(target_workspace, extension_id)

        if output:
            # Write content to file
            output.write_text(extension.content or "")
            console.print(f"[green]Content written to {output}[/green]")
        else:
            # Display details
            status = (
                "[green]active[/green]"
                if extension.active
                else "[yellow]pending[/yellow]"
            )
            console.print(f"[bold]Extension {extension.id}[/bold]")
            console.print(f"  Label: {extension.label}")
            console.print(f"  Matrix: {extension.target_matrix_uri}")
            console.print(f"  Status: {status}")
            console.print(f"  Owner: {extension.owner_username}")
            console.print(
                f"  Created: {extension.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            console.print(
                f"  Updated: {extension.updated_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            console.print()
            console.print("[bold]Content:[/bold]")
            console.print(extension.content or "[dim]<empty>[/dim]")

    except ServiceError as e:
        console.print(f"[red]Failed to get extension: {e}[/red]")
        logger.exception("Failed to get extension")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error getting extension")
        raise typer.Exit(1) from e


@app.command()
def update(
    extension_id: str = typer.Argument(..., help="The extension ID."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Path to new Turtle content file.",
        exists=True,
        readable=True,
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        "-l",
        help="New label for the extension.",
    ),
):
    """Update an extension's content and/or label.

    At least one of --file or --label must be provided.
    Triggers partial re-deployment of the target matrix.
    The extension will be marked as 'pending' until the assembly completes.
    """
    if not file and not label:
        console.print("[red]At least one of --file or --label must be provided.[/red]")
        raise typer.Exit(1)

    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        console.print(f"[dim]Updating extension {extension_id}...[/dim]")

        extension = client.update_extension(
            target_workspace,
            extension_id,
            file,
            label=label,
        )

        status = (
            "[green]active[/green]" if extension.active else "[yellow]pending[/yellow]"
        )

        console.print("[green]Extension updated successfully![/green]")
        console.print(f"  ID: [bold]{extension.id}[/bold]")
        console.print(f"  Label: {extension.label}")
        console.print(f"  Status: {status}")
        console.print(
            f"  Updated: {extension.updated_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    except ServiceError as e:
        console.print(f"[red]Failed to update extension: {e}[/red]")
        logger.exception("Failed to update extension")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error updating extension")
        raise typer.Exit(1) from e


@app.command()
def delete(
    extension_id: str = typer.Argument(..., help="The extension ID."),
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
    """Delete an extension.

    Triggers partial re-deployment of the target matrix.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        if not yes:
            confirm = typer.confirm(
                f"Are you sure you want to delete extension {extension_id}?"
            )
            if not confirm:
                console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)

        console.print(f"[dim]Deleting extension {extension_id}...[/dim]")

        client.delete_extension(target_workspace, extension_id)

        console.print("[green]Extension deleted successfully.[/green]")

    except ServiceError as e:
        console.print(f"[red]Failed to delete extension: {e}[/red]")
        logger.exception("Failed to delete extension")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error deleting extension")
        raise typer.Exit(1) from e
