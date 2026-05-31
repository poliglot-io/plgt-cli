import logging
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer
from rich.logging import RichHandler

from plgt.cmd.auth import app as auth_app
from plgt.cmd.configure import app as config_app
from plgt.cmd.extensions import app as extensions_app
from plgt.cmd.format import app as format_app
from plgt.cmd.init import init_command
from plgt.cmd.lifecycle import app
from plgt.cmd.lifecycle import lifecycle_app
from plgt.cmd.lsp import lsp_command
from plgt.cmd.migration import app as migration_app
from plgt.cmd.publish import app as publish_app
from plgt.cmd.schema import app as schema_app
from plgt.cmd.secrets import app as secrets_app
from plgt.cmd.ui import app as ui_app
from plgt.cmd.validate import app as validate_app
from plgt.cmd.variables import app as variables_app
from plgt.core import settings
from plgt.core.exceptions import CLIError
from plgt.core.logs import console, setup_file_logging

logger = logging.getLogger(settings.APP_AUTHOR)


@app.command()
def version():
    """Returns the currently installed version."""

    try:
        v = _pkg_version("plgt")
    except PackageNotFoundError:
        v = "dev"
    logger.info(f"plgt/{v}")


@app.callback()
def configure_logging(
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging."),
    trace: bool = typer.Option(
        False,
        "--trace",
        help="Enable tracebacks in error messages.",
    ),
):
    # Set log level: DEBUG shows all logs, INFO for normal operation
    log_level = logging.DEBUG if debug else logging.INFO

    if debug:
        # Use plain StreamHandler for debug mode - Rich doesn't work well from threads
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handlers = [handler]
    else:
        rich_handler = RichHandler(
            markup=True,
            rich_tracebacks=trace,
            show_level=False,
            show_path=False,
            show_time=False,
            console=console,
        )
        # Console only shows INFO+ in non-debug mode (file handler gets DEBUG)
        rich_handler.setLevel(logging.INFO)
        handlers = [rich_handler]

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        handlers=handlers,
    )

    logging.getLogger().handlers = handlers

    # Always set up file logging AFTER handlers are configured
    # This adds the file handler to root logger
    setup_file_logging(debug=debug)

    # Silence noisy third-party libraries regardless of debug mode
    noisy_loggers = [
        "markdown_it",
        "urllib3",
        "asyncio",
        "httpx",
        "httpcore",
    ]
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    if not debug:
        # In non-debug mode, set root to WARNING but keep file logging at DEBUG
        # Console handlers only show WARNING+, file gets everything
        logging.getLogger().setLevel(logging.DEBUG)  # Allow DEBUG to file

        # Enable plgt app loggers at INFO level for console
        for prefix in ["plgt", settings.APP_AUTHOR]:
            logging.getLogger(prefix).setLevel(logging.DEBUG)


# CLI configuration
app.add_typer(config_app, name="configure")
app.add_typer(auth_app, name="auth")
app.add_typer(extensions_app, name="extension")
app.command(name="lsp")(lsp_command)
app.add_typer(lifecycle_app, name="lifecycle")
app.add_typer(migration_app, name="migration")
app.add_typer(schema_app, name="schema")
app.add_typer(secrets_app, name="secrets")
app.add_typer(variables_app, name="variables")
app.add_typer(ui_app, name="ui")
app.add_typer(validate_app, name="validate")
app.add_typer(format_app, name="format")
# Registry mutations live at the top level for ergonomics: `plgt publish`,
# `plgt yank`, `plgt unyank`. No publish-namespace prefix because these are
# user-facing verbs, not noun-grouped commands.
for command in publish_app.registered_commands:
    app.registered_commands.append(command)
app.command(name="init")(init_command)


def main():
    """Main entry point for the CLI.

    CLIError subclasses (auth, validation, service, resource-not-found, …)
    are user-facing and print a friendly single-line message — never a
    stack trace — and exit with the error's declared exit_code. Pass
    ``--trace`` to surface the underlying traceback when debugging.
    Anything else is genuinely unexpected; we let typer's default
    handler print it.
    """
    trace_enabled = "--trace" in sys.argv
    try:
        app()
    except CLIError as e:
        if trace_enabled:
            raise
        from plgt.core.logs import console as _console

        _console.print(f"[red]error:[/red] {e}")
        sys.exit(e.exit_code)


if __name__ == "__main__":
    main()
