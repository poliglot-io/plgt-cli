"""Variables commands for Poliglot CLI.

This module implements the CLI commands for managing workspace variables.
Variables are declared by matrices and carry plaintext values (unlike
secrets, whose values are E2E encrypted). Each variable is addressed by a
full URI or a ``prefix:localName`` QName; the command resolves that
identifier to the variable's id before calling the value endpoint.
"""

import logging
import re

import typer
from rich.console import Console
from rich.table import Table

from plgt.clients.variables_client import VariablesClient
from plgt.core import config
from plgt.core.exceptions import ResourceNotFoundError, ServiceError, ValidationError
from plgt.models.variable import Variable

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(help="Variable management operations.")

# A QName is ``prefix:localName`` where both segments are NCName-ish: a letter
# or underscore followed by letters, digits, hyphens, underscores, or dots. We
# validate the shape locally so a malformed ref fails fast with a clear message
# instead of after a network round trip.
_QNAME_RE = re.compile(
    r"^[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*$",
)


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


def _get_client() -> VariablesClient:
    """Get authenticated variables client."""
    session = config.get_session()
    if not session.authenticated:
        console.print("[red]Not authenticated. Run 'plgt auth login' first.[/red]")
        raise typer.Exit(1)
    return VariablesClient(session)


def _validate_identifier(variable_id: str) -> None:
    """Validate that ``variable_id`` is a well-formed full URI or QName.

    Raises ``ValidationError`` for anything that is neither, so the command
    rejects malformed input before listing the workspace's variables.
    """
    if "://" in variable_id:
        return
    if not _QNAME_RE.match(variable_id):
        msg = (
            f"'{variable_id}' is not a valid variable identifier; expected a "
            "full URI (e.g. 'https://example.com/matrix#MyVariable') or a "
            "'prefix:localName' QName (e.g. 'plgt:DefaultFastModel')"
        )
        raise ValidationError(msg)


def _local_name(uri: str) -> str:
    """Best-effort local name extraction from a URI.

    Splits on the fragment separator first, then the final path segment, so
    both ``https://example.com/ns#Foo`` and ``https://example.com/ns/Foo``
    yield ``Foo``. Falls back to the full URI when neither separator exists.
    """
    return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _resolve_variable(variable_id: str, variables: list[Variable]) -> Variable:
    """Resolve a full URI or QName to one of the workspace's variables.

    A full URI must match a declared variable's ``uri`` exactly. A QName or
    bare local name matches when its local part uniquely identifies one
    declared variable (mirroring how install-time binding refs resolve).

    Raises ``ResourceNotFoundError`` when nothing matches and
    ``ValidationError`` when the local name is ambiguous.
    """
    if "://" in variable_id:
        for variable in variables:
            if variable.uri == variable_id:
                return variable
        msg = f"Variable '{variable_id}' not found."
        raise ResourceNotFoundError(msg)

    # QName (prefix:localName) or bare localName: match on the local part.
    local = variable_id.rsplit(":", 1)[-1]
    matches = [v for v in variables if _local_name(v.uri) == local]
    if not matches:
        msg = f"Variable '{variable_id}' not found."
        raise ResourceNotFoundError(msg)
    if len(matches) > 1:
        uris = ", ".join(sorted(v.uri for v in matches))
        msg = (
            f"Variable '{variable_id}' is ambiguous (matches: {uris}); "
            "use the full URI."
        )
        raise ValidationError(msg)
    return matches[0]


@app.command(name="list")
def list_variables(
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
):
    """List all variables in a workspace.

    Shows variables declared by matrices with their current value status.
    """
    try:
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        variables = client.list_variables(target_workspace)

        if not variables:
            console.print("[yellow]No variables found.[/yellow]")
            raise typer.Exit(0)

        table = Table(title=f"Variables in '{target_workspace}'", show_lines=True)
        table.add_column("URI", style="dim", no_wrap=True)
        table.add_column("Label")
        table.add_column("Value")
        table.add_column("Required")

        for variable in variables:
            value = variable.value if variable.has_value else "-"
            required = (
                "[green]Yes[/green]" if variable.required else "[yellow]No[/yellow]"
            )
            table.add_row(
                variable.uri,
                variable.label or "-",
                value,
                required,
            )

        console.print(table)

    except ServiceError as e:
        console.print(f"[red]Failed to list variables: {e}[/red]")
        logger.exception("Failed to list variables")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error listing variables")
        raise typer.Exit(1) from e


@app.command()
def get(
    variable_id: str = typer.Argument(
        ..., help="The variable URI or QName (e.g., plgt:DefaultFastModel)."
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
):
    """Get a variable's metadata and current value."""
    try:
        _validate_identifier(variable_id)
        target_workspace = _get_workspace(workspace)
        client = _get_client()

        variables = client.list_variables(target_workspace)
        try:
            variable = _resolve_variable(variable_id, variables)
        except ResourceNotFoundError:
            console.print(f"[red]Variable '{variable_id}' not found.[/red]")
            raise typer.Exit(1) from None

        value = variable.value if variable.has_value else "-"
        has_value = "Yes" if variable.has_value else "No"
        required = "Yes" if variable.required else "No"

        console.print(f"[bold]Variable: {variable.uri}[/bold]")
        console.print(f"  Id:           {variable.id}")
        console.print(f"  Label:        {variable.label or '-'}")
        console.print(f"  Type:         {variable.variable_type or '-'}")
        console.print(f"  Required:     {required}")
        console.print(f"  Has Value:    {has_value}")
        console.print(f"  Value:        {value}")

    except ValidationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Failed to get variable: {e}[/red]")
        logger.exception("Failed to get variable")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error getting variable")
        raise typer.Exit(1) from e


@app.command(name="set")
def set_variable(
    variable_id: str = typer.Argument(
        ..., help="The variable URI or QName (e.g., plgt:DefaultFastModel)."
    ),
    value: str | None = typer.Argument(
        None,
        help="The value to set. Omit when using --clear.",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Target workspace. Uses default workspace if not specified.",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Clear the variable's value (sends null). Only optional variables can be cleared.",
    ),
):
    """Set a variable's value.

    Provide a VALUE to set, or pass --clear to unset an optional variable.
    The variable identifier accepts a full URI or a 'prefix:localName' QName.
    """
    try:
        _validate_identifier(variable_id)

        if clear and value is not None:
            msg = "Cannot pass both a VALUE and --clear."
            raise ValidationError(msg)
        if not clear and value is None:
            msg = "No value provided. Pass a VALUE or use --clear."
            raise ValidationError(msg)

        target_workspace = _get_workspace(workspace)
        client = _get_client()

        variables = client.list_variables(target_workspace)
        try:
            variable = _resolve_variable(variable_id, variables)
        except ResourceNotFoundError:
            console.print(f"[red]Variable '{variable_id}' not found.[/red]")
            raise typer.Exit(1) from None

        if clear and variable.required:
            msg = f"Variable '{variable_id}' is required and cannot be cleared."
            raise ValidationError(msg)

        new_value = None if clear else value
        client.set_variable_value(target_workspace, variable.id, new_value)

        if clear:
            console.print("[green]Variable value cleared.[/green]")
        else:
            console.print("[green]Variable value updated.[/green]")

    except ValidationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ServiceError as e:
        console.print(f"[red]Failed to set variable: {e}[/red]")
        logger.exception("Failed to set variable")
        raise typer.Exit(1) from e
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        logger.exception("Unexpected error setting variable")
        raise typer.Exit(1) from e
