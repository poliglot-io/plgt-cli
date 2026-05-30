"""UI component commands - wrappers for poliglot-ui CLI."""

import logging
from pathlib import Path

import typer

from plgt.core import settings
from plgt.services.build_service import create_build_config
from plgt.services.ui_build_service import build_ui_components

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
    import subprocess  # noqa: PLC0415 — local import keeps top-of-file lighter

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
    directory: str = typer.Option(
        ".",
        "--dir",
        "-d",
        help="Project directory containing poliglot.yml.",
    ),
    matrix: str | None = typer.Option(
        None,
        "--matrix",
        "-m",
        help=(
            "Build only the named matrix. Defaults to every matrix in the "
            "project that declares 'components:'."
        ),
    ),
):
    """Build UI component bundles for matrices with components configured.

    Reads ``poliglot.yml`` and invokes the underlying ``poliglot-ui build``
    once per matrix that declares ``components:``. Writes the bundle to
    ``<matrix>/<outputDir>/dist/components.js`` and the generated TTL to
    ``<matrix>/<outputDir>/generated/components.ttl``.

    Use ``plgt build`` to run this as part of the full package build —
    this subcommand exists for the iteration loop when you just want the
    JS bundle rebuilt.

    Example:
        plgt ui build
        plgt ui build --matrix linear-issues
    """
    project_dir = Path(directory).resolve()
    config_path = project_dir / "poliglot.yml"
    if not config_path.exists():
        logger.error(f"No poliglot.yml in {project_dir}")
        raise typer.Exit(1)

    try:
        package_config = create_build_config(config_path)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to parse {config_path}: {e}")  # noqa: TRY400
        raise typer.Exit(1) from None

    matrices_with_components = [
        m for m in package_config.matrices if m.components is not None
    ]
    if matrix is not None:
        matrices_with_components = [
            m for m in matrices_with_components if m.name == matrix
        ]
        if not matrices_with_components:
            logger.error(
                f"No matrix named '{matrix}' with components configured in {config_path}"
            )
            raise typer.Exit(1)

    if not matrices_with_components:
        logger.info(
            "No matrices with 'components:' configured. Add a components block "
            "to a matrix in poliglot.yml to build UI."
        )
        return

    had_failure = False
    for matrix_config in matrices_with_components:
        matrix_dir = project_dir / matrix_config.path
        output_dir = matrix_dir / matrix_config.output_dir
        logger.info(
            f"Building UI components for '{matrix_config.name}' "
            f"({matrix_config.components.source}/{matrix_config.components.entry})..."
        )
        result = build_ui_components(
            matrix_dir=matrix_dir,
            components_source=str(matrix_config.components.source),
            components_entry=matrix_config.components.entry,
            output_dir=output_dir,
            # No matrix_uri here: we'd need to assemble the matrix to know
            # it. Skip TTL generation; full plgt build does it.
            matrix_uri=None,
        )
        if result.success:
            logger.info(
                f"  ✓ {matrix_config.name}: {len(result.exports)} export(s) → {result.bundle_path}"
            )
        else:
            had_failure = True
            logger.error(
                f"  ✗ {matrix_config.name}: {result.error}"
            )

    if had_failure:
        raise typer.Exit(1)
