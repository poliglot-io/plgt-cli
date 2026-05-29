"""SHACL validation report display.

This module handles displaying SHACL validation reports
in a user-friendly format.
"""

from rich.console import Console

from plgt.models.lifecycle_command import ValidationEntry, ValidationReport


def _format_violation(entry: ValidationEntry) -> str:
    """Format a single violation for display.

    Args:
        entry: Validation entry with focus_node, path, message, value

    Returns:
        Formatted error message string
    """
    if entry.focus_node and entry.focus_node not in ("", "None", "N/A"):
        node_str = f"Resource '{entry.focus_node}': "
    else:
        node_str = ""

    prop_str = f"Property '{entry.path}'" if entry.path else "[unknown property]"

    if entry.value:
        value_str = f"has invalid value '{entry.value}'"
    else:
        value_str = "is missing or empty"

    message = entry.message or "Constraint violation"
    return f"{node_str}{prop_str} {value_str}: {message}"


def _format_warning(entry: ValidationEntry) -> str:
    """Format a single warning for display.

    Args:
        entry: Validation entry with focus_node, path, message, value

    Returns:
        Formatted warning message string
    """
    if entry.focus_node and entry.focus_node not in ("", "None", "N/A"):
        node_str = f"Resource '{entry.focus_node}': "
    else:
        node_str = ""

    prop_str = f"Property '{entry.path}'" if entry.path else "[unknown property]"
    value_str = f"'{entry.value}'" if entry.value else ""
    message = entry.message or "Constraint violation"

    if value_str:
        return f"{node_str}{prop_str} has value {value_str}: {message}"
    return f"{node_str}{prop_str}: {message}"


def _format_info(entry: ValidationEntry) -> str:
    """Format a single info entry for display.

    Args:
        entry: Validation entry with focus_node, path, message, value

    Returns:
        Formatted info message string
    """
    if entry.focus_node and entry.focus_node not in ("", "None", "N/A"):
        node_str = f"Resource '{entry.focus_node}': "
    else:
        node_str = ""

    prop_str = f"Property '{entry.path}'" if entry.path else ""
    message = entry.message or "Info"

    if prop_str:
        return f"{node_str}{prop_str}: {message}"
    return f"{node_str}{message}"


def display_validation_report(report: ValidationReport, console: Console) -> None:
    """Display a SHACL validation report.

    Args:
        report: ValidationReport object from the API
        console: Rich console for output
    """
    # Display violations
    if report.violations:
        console.print(f"[bold][red]{report.violation_count} violations[/red][/bold]")
        for entry in report.violations:
            msg = _format_violation(entry)
            console.print(f"  [red]✗[/red] {msg}")

    # Display warnings
    if report.warnings:
        if report.violations:
            console.print()  # Add spacing between sections
        console.print(f"[bold][yellow]{report.warning_count} warnings[/yellow][/bold]")
        for entry in report.warnings:
            msg = _format_warning(entry)
            console.print(f"  [yellow]⚠[/yellow] {msg}")

    # Display infos
    if report.infos:
        if report.violations or report.warnings:
            console.print()  # Add spacing between sections
        console.print(f"[bold][blue]{report.info_count} info[/blue][/bold]")
        for entry in report.infos:
            msg = _format_info(entry)
            console.print(f"  [blue]ℹ[/blue] {msg}")  # noqa: RUF001
