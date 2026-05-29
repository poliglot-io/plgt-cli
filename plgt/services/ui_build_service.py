"""UI build service for matrix compilation.

This module handles building UI components and generating RDF for plgt-ui:Bundle and plgt-ui:Component.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from plgt.core import settings
from plgt.services.template_service import render_template

logger = logging.getLogger(settings.APP_AUTHOR)


@dataclass
class PoliglotUIConfig:
    """Configuration extracted from poliglot.yml for UI components."""

    components_source: str
    components_entry: str
    output_dir: str


@dataclass
class UIBuildResult:
    """Result of UI build operation."""

    bundle_path: Path
    generated_ttl: Path
    exports: list[str]
    success: bool
    error: str | None = None


def load_poliglot_config(project_dir: Path) -> PoliglotUIConfig | None:
    """Load UI config from poliglot.yml if it exists and has components.

    Args:
        project_dir: Project root directory

    Returns:
        PoliglotUIConfig if components are configured, None otherwise
    """
    yml_path = project_dir / "poliglot.yml"
    yaml_path = project_dir / "poliglot.yaml"

    config_path = (
        yml_path if yml_path.exists() else (yaml_path if yaml_path.exists() else None)
    )

    if not config_path:
        return None

    with config_path.open() as f:
        config = yaml.safe_load(f)

    if not config or "components" not in config:
        return None

    components = config["components"]

    return PoliglotUIConfig(
        components_source=components.get("source", "./src/components"),
        components_entry=components.get("entry", "index.ts"),
        output_dir=config.get("outputDir", "./.matrix"),
    )


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


def build_ui_if_needed(
    project_dir: Path, matrix_uri: str | None = None
) -> UIBuildResult | None:
    """Build UI components if poliglot.yml has components configured.

    Args:
        project_dir: Project root directory

    Returns:
        UIBuildResult if UI was built, None if no UI components
    """
    config = load_poliglot_config(project_dir)
    if not config:
        return None

    # Normalize output_dir path
    output_dir_str = config.output_dir
    if output_dir_str.startswith("./"):
        output_dir_str = output_dir_str[2:]
    output_dir = project_dir / output_dir_str
    dist_dir = output_dir / "dist"
    generated_dir = output_dir / "generated"

    # Ensure directories exist
    dist_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    # Check if entry point exists
    entry_point = (
        project_dir / config.components_source.lstrip("./") / config.components_entry
    )
    if not entry_point.exists():
        logger.warning(f"UI entry point not found: {entry_point}")
        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=generated_dir / "components.ttl",
            exports=[],
            success=False,
            error=f"Entry point not found: {entry_point}",
        )

    # Run poliglot-ui build
    try:
        result = subprocess.run(  # noqa: S603
            [*settings.UIKIT_COMMAND, "build", "--json", "-d", str(project_dir)],
            check=False,
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=60,
        )

        if result.returncode != 0:
            error = result.stderr or result.stdout
            logger.error(f"UI build failed: {error}")
            return UIBuildResult(
                bundle_path=dist_dir / "components.js",
                generated_ttl=generated_dir / "components.ttl",
                exports=[],
                success=False,
                error=error,
            )

        # Parse JSON output
        build_result = json.loads(result.stdout)
        exports = build_result.get("exports", [])

        # Generate components.ttl with matrix URI
        if matrix_uri:
            ttl_content = generate_component_ttl(exports, matrix_uri)
            ttl_path = generated_dir / "components.ttl"
            ttl_path.write_text(ttl_content)
        else:
            ttl_path = generated_dir / "components.ttl"
            # Skip TTL generation if no matrix URI provided
            logger.warning("No matrix URI provided, skipping components.ttl generation")

        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=ttl_path,
            exports=exports,
            success=True,
        )

    except subprocess.TimeoutExpired:
        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=generated_dir / "components.ttl",
            exports=[],
            success=False,
            error="UI build timed out after 60 seconds",
        )
    except json.JSONDecodeError as e:
        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=generated_dir / "components.ttl",
            exports=[],
            success=False,
            error=f"Failed to parse UI build output: {e}",
        )
    except FileNotFoundError:
        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=generated_dir / "components.ttl",
            exports=[],
            success=False,
            error=f"{settings.UIKIT_COMMAND[0]} not found - is Node.js installed?",
        )
    except OSError as e:
        return UIBuildResult(
            bundle_path=dist_dir / "components.js",
            generated_ttl=generated_dir / "components.ttl",
            exports=[],
            success=False,
            error=str(e),
        )
