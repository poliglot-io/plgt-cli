"""UI build service for matrix compilation.

This module handles building UI components and generating RDF for
plgt-ui:Bundle and plgt-ui:Component. Invokes the `poliglot-ui` CLI
(from the @poliglot-io/uikit npm package) with explicit arguments —
the user's components config lives in poliglot.yml under each
matrix's block and is already parsed into a MatrixBuildConfig by
build_service.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from plgt.core import settings
from plgt.services.template_service import render_template

logger = logging.getLogger(settings.APP_AUTHOR)


@dataclass
class UIBuildResult:
    """Result of UI build operation."""

    bundle_path: Path
    generated_ttl: Path
    exports: list[str]
    success: bool
    error: str | None = None


def generate_component_ttl(exports: list[str], matrix_uri: str) -> str:
    """Generate components.ttl content from exports list.

    Args:
        exports: List of export names from index.ts
        matrix_uri: Matrix URI prefix for the components

    Returns:
        Turtle content for plgt-ui:Bundle and plgt-ui:Component definitions
    """
    return render_template(
        "components.ttl.j2",
        matrix_uri=matrix_uri,
        exports=exports,
    )


def build_ui_components(
    matrix_dir: Path,
    components_source: str,
    components_entry: str,
    output_dir: Path,
    matrix_uri: str | None = None,
) -> UIBuildResult:
    """Build UI components for a matrix.

    Args:
        matrix_dir: Path to the matrix directory (where components_source is
            resolved against).
        components_source: Relative path from matrix_dir to the components
            source directory (e.g. ``./src/components``).
        components_entry: Entry file name relative to components_source
            (e.g. ``index.ts``).
        output_dir: Absolute path to the matrix's output directory.
            ``dist/components.js`` and ``generated/components.ttl`` are
            written under it.
        matrix_uri: Matrix URI prefix used to generate components.ttl. If
            None, the TTL file is not written.

    Returns:
        UIBuildResult describing the outcome.
    """
    dist_dir = output_dir / "dist"
    generated_dir = output_dir / "generated"
    bundle_path = dist_dir / "components.js"
    ttl_path = generated_dir / "components.ttl"

    dist_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    entry_point = (
        matrix_dir / components_source.lstrip("./") / components_entry
    ).resolve()
    if not entry_point.exists():
        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=[],
            success=False,
            error=f"Entry point not found: {entry_point}",
        )

    # Compute paths relative to matrix_dir for the poliglot-ui CLI (it
    # resolves --entry and --out against its --dir option).
    rel_entry = entry_point.relative_to(matrix_dir.resolve())
    rel_out = bundle_path.relative_to(matrix_dir.resolve())

    try:
        result = subprocess.run(  # noqa: S603
            [
                *settings.UIKIT_COMMAND,
                "build",
                "--json",
                "-d",
                str(matrix_dir),
                "--entry",
                str(rel_entry),
                "--out",
                str(rel_out),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=matrix_dir,
            timeout=60,
        )

        if result.returncode != 0:
            error = result.stderr or result.stdout
            logger.error(f"UI build failed: {error}")
            return UIBuildResult(
                bundle_path=bundle_path,
                generated_ttl=ttl_path,
                exports=[],
                success=False,
                error=error,
            )

        build_result = json.loads(result.stdout)
        exports = build_result.get("exports", [])

        if matrix_uri:
            ttl_path.write_text(generate_component_ttl(exports, matrix_uri))
        else:
            logger.warning("No matrix URI provided, skipping components.ttl generation")

        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=exports,
            success=True,
        )

    except subprocess.TimeoutExpired:
        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=[],
            success=False,
            error="UI build timed out after 60 seconds",
        )
    except json.JSONDecodeError as e:
        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=[],
            success=False,
            error=f"Failed to parse UI build output: {e}",
        )
    except FileNotFoundError:
        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=[],
            success=False,
            error=f"{settings.UIKIT_COMMAND[0]} not found — is Node.js installed?",
        )
    except OSError as e:
        return UIBuildResult(
            bundle_path=bundle_path,
            generated_ttl=ttl_path,
            exports=[],
            success=False,
            error=str(e),
        )
