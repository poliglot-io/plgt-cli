"""Init command for Poliglot CLI.

Creates a new matrix project with interactive prompts.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from plgt.core import settings
from plgt.services.template_service import render_template

logger = logging.getLogger(settings.APP_AUTHOR)
console = Console()

VSCODE_FORMAT_SETTINGS = {
    "[turtle]": {
        "editor.defaultFormatter": "poliglot.plgt",
        "editor.formatOnSave": True,
    },
    "[sparql]": {
        "editor.defaultFormatter": "poliglot.plgt",
        "editor.formatOnSave": True,
    },
}

# Published marketplace id for the extension. When publishing isn't done
# (or the user is offline), fall back to the bundled .vsix shipped with
# this CLI distribution.
VSCODE_EXTENSION_ID = "poliglot.plgt"


def _setup_vscode(project_dir: Path, *, install_extension: bool | None) -> None:
    """Write ``<project>/.vscode/settings.json`` with the format-on-save
    block, and (optionally) install the ``poliglot.plgt`` extension via
    the ``code`` CLI.

    ``install_extension`` is a tri-state:
    * ``True`` — install without prompting (used by ``--vscode``)
    * ``False`` — skip extension install (used by ``--no-vscode-extension``)
    * ``None`` — prompt the user interactively

    Existing settings.json files are merged key-by-key when they're plain
    JSON. When they contain comments (JSONC), we leave them alone and
    print a one-line note — silently rewriting a file the user has
    customised would be hostile.
    """
    settings_path = project_dir / ".vscode" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Start from an empty base when the file doesn't exist yet; merge
    # over the parsed contents when it does. ``skip_write`` signals that
    # the existing file is JSONC (with comments) and we shouldn't touch
    # it — printing manual instructions instead.
    existing: dict = {}
    skip_write = False
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                console.print(
                    "[yellow]  .vscode/settings.json contains comments — "
                    "skipping merge. Add these manually:[/yellow]"
                )
                console.print(f"    {json.dumps(VSCODE_FORMAT_SETTINGS, indent=2)}")
                skip_write = True

    if not skip_write:
        merged = {**existing}
        for key, value in VSCODE_FORMAT_SETTINGS.items():
            # Per-language scopes get key-level merge so we don't trample
            # other editor settings the user might keep there.
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        console.print("[green]  Wrote .vscode/settings.json[/green]")

    # Warn if .gitignore would silently exclude the new settings file.
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        ignore_lines = gitignore.read_text(encoding="utf-8").splitlines()
        # The exact ignore patterns that would swallow .vscode/settings.json
        # without an explicit allowlist rule.
        blanket_ignore = any(
            line.strip() in (".vscode/", ".vscode/*", ".vscode")
            for line in ignore_lines
        )
        has_allowlist = any(
            line.strip() == "!.vscode/settings.json" for line in ignore_lines
        )
        if blanket_ignore and not has_allowlist:
            console.print(
                "[yellow]  Note: your .gitignore ignores .vscode/. "
                "Update to allowlist style to share the settings:[/yellow]"
            )
            console.print("    .vscode/*")
            console.print("    !.vscode/settings.json")

    # Extension install via `code` CLI. Skipped entirely if the CLI isn't
    # on PATH — we surface a one-line note instead.
    code_cli = shutil.which("code")
    if code_cli is None:
        console.print(
            "[yellow]  VS Code CLI (`code`) not on PATH — install the "
            "'Poliglot' extension manually from the marketplace.[/yellow]"
        )
        return

    if install_extension is None:
        install_extension = Confirm.ask(
            f"  Install the {VSCODE_EXTENSION_ID} extension?", default=True
        )
    if not install_extension:
        return

    # Try the marketplace first; fall back to the bundled .vsix when the
    # extension isn't published yet (or the user is offline).
    if _install_extension_from_marketplace(code_cli):
        console.print(
            f"[green]  Installed {VSCODE_EXTENSION_ID} from the marketplace.[/green]"
        )
        return

    vsix = _bundled_vsix_path()
    if vsix is None:
        console.print(
            "[yellow]  Marketplace install failed and no bundled .vsix is "
            "available — install the extension manually.[/yellow]"
        )
        return
    if _install_extension_from_vsix(code_cli, vsix):
        console.print(f"[green]  Installed {VSCODE_EXTENSION_ID} from {vsix}[/green]")
    else:
        console.print(
            "[yellow]  Could not install the extension. Run "
            f"`code --install-extension {vsix}` manually.[/yellow]"
        )


def _install_extension_from_marketplace(code_cli: str) -> bool:
    result = subprocess.run(  # noqa: S603
        [code_cli, "--install-extension", VSCODE_EXTENSION_ID],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _install_extension_from_vsix(code_cli: str, vsix: Path) -> bool:
    result = subprocess.run(  # noqa: S603
        [code_cli, "--install-extension", str(vsix)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _bundled_vsix_path() -> Path | None:
    """Locate the .vsix shipped with the plgt distribution.

    The VSIX is packaged under ``plgt/editors/vscode/poliglot-latest.vsix`` as
    package-data (see ``pyproject.toml``). We resolve it via
    ``importlib.resources`` so the lookup works identically for editable
    installs, wheels, and zipped distributions.

    Returns None when the bundled VSIX is missing — caller surfaces a
    helpful note instead of failing the init flow."""
    from importlib.resources import as_file, files

    try:
        resource = files("plgt.editors.vscode") / "poliglot-latest.vsix"
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    if not resource.is_file():
        return None
    # ``as_file`` materializes the resource on disk when needed (e.g. from
    # inside a zip). The context manager would normally clean up after
    # itself, but the VSCode CLI reads the file synchronously so it's safe
    # to exit the context before the caller uses the path.
    with as_file(resource) as path:
        return Path(path)


def init_command(
    directory: Path = typer.Option(
        None,
        "--dir",
        "-d",
        help="Project directory. Defaults to current directory.",
    ),
    vscode: bool | None = typer.Option(
        None,
        "--vscode/--no-vscode",
        help=(
            "Set up VS Code integration (format-on-save for .ttl/.rq, "
            "install the poliglot.plgt extension). Without this flag the "
            "user is prompted interactively."
        ),
    ),
):
    """Initialize a new matrix project.

    Writes a minimal ``poliglot.yml`` and ``.gitignore`` in the project
    directory and (optionally) sets up VS Code integration. Spec authoring
    is left to the user; ``plgt validate`` will run once they add their
    own ``spec/*.ttl`` files.
    """
    project_dir = directory or Path.cwd()
    project_dir = project_dir.resolve()

    console.print(
        f"\n[bold blue]Initializing matrix project in: {project_dir}[/bold blue]\n"
    )

    default_name = project_dir.name
    name = Prompt.ask("Matrix name", default=default_name)

    console.print()

    # Generate poliglot.yml
    poliglot_path = project_dir / "poliglot.yml"
    if poliglot_path.exists():
        console.print("[yellow]  poliglot.yml already exists, skipping[/yellow]")
    else:
        content = render_template(
            "poliglot.yml.j2",
            name=name,
            version="0.1.0",
            engine_version=">=1 <2",
            spec_patterns=["./spec"],
            artifact_patterns=["./spec/artifacts"],
            components=None,
            output_dir="./.matrix",
        )
        poliglot_path.write_text(content)
        console.print("[green]  Created poliglot.yml[/green]")

    # Generate .gitignore
    gitignore_path = project_dir / ".gitignore"
    if gitignore_path.exists():
        existing = gitignore_path.read_text()
        if ".matrix" not in existing:
            content = render_template("gitignore.j2", output_dir="./.matrix")
            gitignore_path.write_text(existing + "\n" + content)
            console.print("[green]  Updated .gitignore[/green]")
    else:
        content = render_template("gitignore.j2", output_dir="./.matrix")
        gitignore_path.write_text(content)
        console.print("[green]  Created .gitignore[/green]")

    # VS Code integration — format-on-save + extension install.
    if vscode is None:
        console.print("\n[bold blue]VS Code integration[/bold blue]")
        setup_vscode = Confirm.ask(
            "  Set up VS Code (format-on-save for .ttl/.rq, install extension)?",
            default=True,
        )
    else:
        setup_vscode = vscode
        if setup_vscode:
            console.print("\n[bold blue]VS Code integration[/bold blue]")
    if setup_vscode:
        # When --vscode was passed, also install the extension non-interactively;
        # when prompting, _setup_vscode asks separately.
        install_pref = True if vscode is True else None
        _setup_vscode(project_dir, install_extension=install_pref)

    console.print("\n[bold green]Matrix project initialized![/bold green]")
    console.print("\nNext steps:")
    console.print("  1. Add your matrix spec under [cyan]spec/[/cyan]")
    console.print("  2. Run [cyan]plgt sync[/cyan] to populate the local dep cache")
    console.print("  3. Run [cyan]plgt validate[/cyan] to check your spec")
    console.print("  4. Run [cyan]plgt build[/cyan] to compile a package")
