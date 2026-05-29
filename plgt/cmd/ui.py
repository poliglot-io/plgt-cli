"""UI component commands - wrappers for poliglot-ui CLI."""

import logging
import subprocess

import typer

from plgt.core import settings

logger = logging.getLogger(__name__)

app = typer.Typer(help="UI component development commands")


@app.command()
def preview(
    port: int = typer.Option(3333, "--port", "-p", help="Server port"),
    directory: str = typer.Option(".", "--dir", "-d", help="Project directory"),
    host: str = typer.Option("localhost", "--host", help="Server host"),
):
    """Start dev server to preview components.

    Reads poliglot.preview.js config from your project root and serves
    a preview app where you can browse and inspect component variants.

    Example:
        plgt ui preview
        plgt ui preview --port 4000
    """
    cmd = [
        *settings.UIKIT_COMMAND,
        "preview",
        "-d",
        directory,
        "-p",
        str(port),
        "--host",
        host,
    ]

    logger.info(f"Starting preview server on {host}:{port}...")

    try:
        subprocess.run(cmd, check=True)  # noqa: S603 - command from trusted settings
    except subprocess.CalledProcessError as e:
        logger.error(f"Preview server failed with exit code {e.returncode}")  # noqa: TRY400
        raise typer.Exit(1) from None
    except FileNotFoundError:
        logger.error(
            "poliglot-ui command not found. Ensure @poliglot-io/uikit is installed."
        )  # noqa: TRY400
        raise typer.Exit(1) from None


@app.command()
def build(
    directory: str = typer.Option(".", "--dir", "-d", help="Project directory"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Build component bundle.

    Compiles TSX components to a single JavaScript bundle
    for use in Poliglot OS.

    Example:
        plgt ui build
        plgt ui build --json
    """
    cmd = [*settings.UIKIT_COMMAND, "build", "-d", directory]

    if json_output:
        cmd.append("--json")

    try:
        subprocess.run(cmd, check=True)  # noqa: S603 - command from trusted settings
    except subprocess.CalledProcessError as e:
        logger.error(f"Build failed with exit code {e.returncode}")  # noqa: TRY400
        raise typer.Exit(1) from None
    except FileNotFoundError:
        logger.error(
            "poliglot-ui command not found. Ensure @poliglot-io/uikit is installed."
        )  # noqa: TRY400
        raise typer.Exit(1) from None
