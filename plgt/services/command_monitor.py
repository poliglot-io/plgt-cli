"""Real-time command event monitoring.

This module provides functionality for monitoring lifecycle command events
and displaying them in a live-updating console view.
"""

import logging
import time

from rich.console import Console
from rich.live import Live
from rich.text import Text

from plgt.clients.lifecycle_command_client import LifecycleCommandClient
from plgt.core import settings
from plgt.core.exceptions import ServiceError
from plgt.models.lifecycle_command import LifecycleCommandStatus
from plgt.services.validation_report import display_validation_report

logger = logging.getLogger(settings.APP_AUTHOR)

# Event level styling: (icon, color)
LEVEL_STYLES = {
    "INFO": ("ℹ", "blue"),  # noqa: RUF001
    "SUCCESS": ("✓", "green"),
    "WARNING": ("⚠", "yellow"),
    "ERROR": ("✗", "red"),
}

# Terminal statuses that end monitoring
TERMINAL_STATUSES = {LifecycleCommandStatus.COMPLETED, LifecycleCommandStatus.FAILED}


def _get_current_status(command) -> str:
    """Extract status string from command object.

    Args:
        command: LifecycleCommand object with status attribute

    Returns:
        Status string (e.g., "COMPLETED", "FAILED")
    """
    if not hasattr(command, "status") or not command.status:
        return "UNKNOWN"

    if hasattr(command.status, "value"):
        return command.status.value
    if isinstance(command.status, str):
        return command.status
    return str(command.status).split(".")[-1]


def _format_event_line(event, seen_event_ids: set) -> str:
    """Format a single event for display.

    Args:
        event: Lifecycle event object
        seen_event_ids: Set of seen event IDs (mutated to add new ones)

    Returns:
        Formatted event line with markup
    """
    if event.id not in seen_event_ids:
        seen_event_ids.add(event.id)

    level_str = event.level.value if hasattr(event.level, "value") else str(event.level)
    icon, color = LEVEL_STYLES.get(level_str, ("•", "white"))

    # Convert to local timezone for display
    event_time = event.created_at
    if event_time.tzinfo is not None:
        event_time = event_time.astimezone()  # Convert to local timezone
    timestamp = event_time.strftime("%H:%M:%S")

    return f"  [dim][{timestamp}][/dim] [{color}]{icon}[/{color}] {event.message}"


def _handle_terminal_status(
    status: str,
    command_client: LifecycleCommandClient,
    workspace: str,
    command_id: str,
    console: Console,
) -> None:
    """Handle terminal status display.

    Args:
        status: Terminal status string
        command_client: Client for API calls
        workspace: Workspace name
        command_id: Command ID
        console: Console for output
    """
    # Always try to fetch and display validation report (shows warnings on success too)
    try:
        report = command_client.get_validation_report(workspace, command_id)
        if report:
            console.print()
            display_validation_report(report, console)
        else:
            logger.debug("No validation report available for command %s", command_id)
    except ServiceError as e:
        logger.debug("Failed to fetch validation report: %s", e)

    console.print()
    if status == "FAILED":
        console.print("[red]Status: FAILED[/red]")
    elif status == "COMPLETED":
        console.print("[green]Status: COMPLETED[/green]")


def monitor_command_events(
    api_session,
    workspace: str,
    command_id: str,
    console: Console,
    polling_interval: float = 2.0,
    timeout: float = 600.0,
) -> str:
    """Monitor command events in real-time until completion.

    Args:
        api_session: Authenticated API session
        workspace: Workspace name
        command_id: Command ID to monitor
        console: Rich console for output
        polling_interval: Seconds between polls (default 2.0)
        timeout: Maximum seconds to wait (default 600 = 10 minutes)

    Returns:
        Final command status (COMPLETED, FAILED, TIMEOUT, or UNKNOWN)
    """
    command_client = LifecycleCommandClient(api_session)
    start_time = time.time()
    seen_event_ids: set = set()

    console.print("[bold]Events:[/bold]")

    final_status = None

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                console.print("[red]Timeout waiting for command to complete[/red]")
                return "TIMEOUT"

            try:
                # Get command status and events
                command = command_client.get_command(workspace, command_id)
                events = command_client.get_command_events(workspace, command_id)

                current_status = _get_current_status(command)

                # Sort events by creation time (oldest first for display)
                events_sorted = sorted(events, key=lambda e: e.created_at)

                # Build log-style output (events only, header is shown above)
                output_lines = [
                    _format_event_line(event, seen_event_ids) for event in events_sorted
                ]

                # Update live display with formatted text
                display_text = Text.from_markup("\n".join(output_lines))
                live.update(display_text)

                # Check for terminal status - exit loop but handle status after Live closes
                if command.status in TERMINAL_STATUSES:
                    final_status = current_status
                    break

                time.sleep(polling_interval)

            except Exception as e:
                logger.exception("Error polling command events")
                console.print(f"[red]Error monitoring command: {e}[/red]")
                return "UNKNOWN"

    # Handle terminal status AFTER the Live display has closed
    if final_status:
        _handle_terminal_status(
            final_status,
            command_client,
            workspace,
            command_id,
            console,
        )
        return final_status

    return "UNKNOWN"
