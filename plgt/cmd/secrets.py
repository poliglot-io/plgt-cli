"""Secrets commands for Poliglot CLI.

This module implements the CLI commands for managing secrets.
Secrets are defined in matrices and their values are managed through
E2E encrypted communication with the Platform API.
"""

import logging
import sys

import typer
from rich.console import Console
from rich.table import Table

from plgt.clients.secrets_client import (
    SCOPE_PRINCIPAL,
    SCOPE_WORKSPACE,
    SecretsClient,
)
from plgt.core import config
from plgt.core.exceptions import ResourceNotFoundError, ServiceError

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(help="Secret management operations.")


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


def _get_client() -> SecretsClient:
    """Get authenticated secrets client."""
    session = config.get_session()
    if not session.authenticated:
        console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
        raise typer.Exit(1)
    return SecretsClient(session)


def _extract_matrix_name(uri: str) -> str:
    """Extract matrix name from secret URI.

    Args:
        uri: Full URI like "https://example.com/matrix#SecretName"

    Returns:
        Matrix name portion (everything between last / and #)
    """
    # Remove fragment if present
    base = uri.split("#", maxsplit=1)[0] if "#" in uri else uri
    # Get last path component
    return base.rstrip("/").split("/")[-1]


@app.command(name="list")
def list_secrets(
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    prefix: str | None = typer.Option(
        None,
        "--prefix",
        "-p",
        help="Filter secrets by URI prefix.",
    ),
):
    """List all secrets in a workspace.

    Shows secrets defined in matrices with their current value status.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        secrets = client.list_secrets(target_workspace, prefix=prefix)

        if not secrets:
            console.print("[yellow]No secrets found.[/yellow]")
            raise typer.Exit(0)

        table = Table(title=f"Secrets in '{target_workspace}'", show_lines=True)
        table.add_column("URI", style="dim", no_wrap=True)
        table.add_column("Matrix")
        table.add_column("Scopes")
        table.add_column("Last Updated")

        for secret in secrets:
            scopes = ", ".join(secret.allowed_scopes) if secret.allowed_scopes else "-"
            updated = secret.updated_at.strftime("%Y-%m-%d %H:%M")
            matrix_name = secret.matrix_name or _extract_matrix_name(secret.uri)

            table.add_row(
                secret.id,
                matrix_name,
                scopes,
                updated,
            )

        console.print(table)

    except ServiceError as e:
        console.print(f"[red]Failed to list secrets: {e}[/red]")
        logger.exception("Failed to list secrets")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error listing secrets")
        raise typer.Exit(1) from e


@app.command()
def get(
    secret_uri: str = typer.Argument(
        ..., help="The secret URI or QName (e.g., matrix:SecretName)."
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    value: bool = typer.Option(
        False,
        "--value",
        "-v",
        help="Retrieve and display the decrypted secret value.",
    ),
):
    """Get a secret's metadata or value.

    By default shows metadata. Use --value to retrieve the decrypted value.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        if value:
            # Fetch and display the decrypted value
            try:
                secret_value = client.get_secret_value(target_workspace, secret_uri)
                console.print(f"Value: {secret_value}")
            except ResourceNotFoundError:
                console.print(f"[red]Secret '{secret_uri}' not found.[/red]")
                raise typer.Exit(1) from None
            except ServiceError as e:
                # Check if the secret has no value set
                if "no value" in str(e).lower():
                    console.print(
                        f"[yellow]Secret '{secret_uri}' has no value set.[/yellow]"
                    )
                    raise typer.Exit(1) from None
                raise
        else:
            # Fetch and display metadata
            try:
                secret = client.get_secret(target_workspace, secret_uri)
            except ResourceNotFoundError:
                console.print(f"[red]Secret '{secret_uri}' not found.[/red]")
                raise typer.Exit(1) from None

            matrix_name = secret.matrix_name or _extract_matrix_name(secret.uri)
            scopes = ", ".join(secret.allowed_scopes) if secret.allowed_scopes else "-"

            console.print(f"[bold]Secret: {secret.id}[/bold]")
            console.print(f"  URI:            {secret.uri}")
            console.print(f"  Matrix:         {matrix_name}")
            console.print(f"  Description:    {secret.description or '-'}")
            console.print(f"  Allowed Scopes: {scopes}")
            console.print(
                f"  Created:        {secret.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )
            console.print(
                f"  Updated:        {secret.updated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )

    except ServiceError as e:
        console.print(f"[red]Failed to get secret: {e}[/red]")
        logger.exception("Failed to get secret")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error getting secret")
        raise typer.Exit(1) from e


@app.command(name="set")
def set_secret(
    secret_uri: str = typer.Argument(
        ..., help="The secret URI or QName (e.g., matrix:SecretName)."
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    from_stdin: bool = typer.Option(
        False,
        "--from-stdin",
        help="Read value from stdin instead of interactive prompt.",
    ),
    scope: str = typer.Option(
        SCOPE_WORKSPACE,
        "--scope",
        help="Scope to write the value at: 'workspace' (shared) or 'principal' (private to you).",
    ),
    scope_entity_id: str | None = typer.Option(
        None,
        "--scope-entity-id",
        help="Principal id to write for. Required when --scope principal.",
    ),
):
    """Set a secret's value.

    Writes to the 'workspace' scope by default (shared by everyone in the
    workspace). Pass --scope principal to set a value private to a single
    principal. By default prompts for the value interactively (masked input);
    use --from-stdin to pipe the value.
    """
    try:
        if scope not in (SCOPE_WORKSPACE, SCOPE_PRINCIPAL):
            console.print(
                f"[red]Invalid --scope '{scope}'. Use '{SCOPE_WORKSPACE}' or '{SCOPE_PRINCIPAL}'.[/red]"
            )
            raise typer.Exit(1)
        if scope == SCOPE_PRINCIPAL and not scope_entity_id:
            console.print(
                "[red]--scope-entity-id is required when --scope principal.[/red]"
            )
            raise typer.Exit(1)

        target_workspace = _get_workspace(workspace)
        client = _get_client()

        # Verify the secret exists first
        try:
            client.get_secret(target_workspace, secret_uri)
        except ResourceNotFoundError:
            console.print(f"[red]Secret '{secret_uri}' not found.[/red]")
            raise typer.Exit(1) from None

        # Get the value
        if from_stdin:
            if sys.stdin.isatty():
                console.print(
                    "[yellow]Warning: --from-stdin specified but stdin is a terminal.[/yellow]"
                )
            secret_value = sys.stdin.read().strip()
            if not secret_value:
                console.print("[red]No value provided via stdin.[/red]")
                raise typer.Exit(1)
        else:
            if not sys.stdin.isatty():
                console.print(
                    "[red]Not in interactive mode. Use --from-stdin to pipe values.[/red]"
                )
                raise typer.Exit(1)
            secret_value = typer.prompt("Enter value", hide_input=True)
            if not secret_value:
                console.print("[red]Value cannot be empty.[/red]")
                raise typer.Exit(1)

        # Set the value at the requested scope
        client.set_secret_value(
            target_workspace,
            secret_uri,
            secret_value,
            scope=scope,
            scope_entity_id=scope_entity_id,
        )
        console.print("[green]Secret value updated.[/green]")

    except ServiceError as e:
        console.print(f"[red]Failed to set secret: {e}[/red]")
        logger.exception("Failed to set secret")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error setting secret")
        raise typer.Exit(1) from e
