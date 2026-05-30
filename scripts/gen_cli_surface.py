#!/usr/bin/env python3
"""Emit a machine-readable description of the plgt CLI surface.

Walks the typer command tree and writes ``surface/cli.json``. The kleo
docs site pulls this artifact at release tags and renders narrative MDX
alongside an auto-generated reference table per command.

Output is deterministic (sorted keys, fixed ordering) so the committed
file is diff-friendly and CI / pre-commit can drift-check by regenerating
and comparing.

Schema is versioned via the top-level ``schemaVersion``. Bump only when
the JSON shape itself changes — adding a CLI command or flag does not
bump the schema, it bumps the ``version`` field (which mirrors the
package version).
"""

from __future__ import annotations

import inspect
import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import click
import typer

SCHEMA_VERSION = "1.0.0"
OUTPUT = Path(__file__).resolve().parent.parent / "surface" / "cli.json"


def _type_name(param: click.Parameter) -> str:
    """Stable, kleo-renderable name for a click parameter's type."""
    t = param.type
    name = getattr(t, "name", None) or type(t).__name__
    # click's "text" is just str, normalize for readability
    return {"text": "string"}.get(name, name)


def _param_to_dict(param: click.Parameter) -> dict[str, Any]:
    opts = list(getattr(param, "opts", []) or [])
    is_flag = bool(getattr(param, "is_flag", False))
    # typer's TyperArgument doesn't subclass click.Argument — duck-type via
    # param_type_name, which is "argument" for positionals and "option" for
    # flags / named options.
    is_positional = getattr(param, "param_type_name", None) == "argument"

    default = param.default
    # typer wraps some defaults in OptionInfo / ArgumentInfo
    if hasattr(default, "default"):
        default = default.default
    if default is inspect.Parameter.empty:
        default = None
    if callable(default):
        default = None

    return {
        "name": param.name,
        "type": _type_name(param),
        "positional": is_positional,
        "flag": is_flag,
        "opts": sorted(opts),
        "required": bool(param.required),
        "default": default,
        "help": (getattr(param, "help", None) or "").strip() or None,
    }


def _command_to_dict(cmd: click.Command, path: list[str]) -> dict[str, Any]:
    return {
        "path": path,
        "shortHelp": (cmd.get_short_help_str() or "").strip() or None,
        "help": (cmd.help or "").strip() or None,
        "deprecated": bool(getattr(cmd, "deprecated", False)),
        "hidden": bool(getattr(cmd, "hidden", False)),
        "params": [
            _param_to_dict(p)
            for p in cmd.params
            if not isinstance(p, click.Parameter) or p.name not in {"help"}
        ],
    }


def _walk(cmd: click.Command, path: list[str], out: list[dict[str, Any]]) -> None:
    sub = getattr(cmd, "commands", None)
    if sub:
        # Group: don't emit the group itself, only its leaves.
        for name in sorted(sub):
            _walk(sub[name], [*path, name], out)
        return
    if cmd.hidden:
        return
    out.append(_command_to_dict(cmd, path))


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _v

    try:
        return _v("plgt")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def build_surface() -> dict[str, Any]:
    # Importing __main__ executes the typer wiring (add_typer calls).
    main_mod = import_module("plgt.__main__")
    app: typer.Typer = main_mod.app

    click_app = typer.main.get_command(app)
    commands: list[dict[str, Any]] = []
    _walk(click_app, [], commands)
    commands.sort(key=lambda c: c["path"])

    return {
        "schemaVersion": SCHEMA_VERSION,
        "cli": "plgt",
        "version": _package_version(),
        "commands": commands,
    }


def main() -> int:
    surface = build_surface()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(surface, indent=2, sort_keys=True) + "\n"

    check = "--check" in sys.argv
    if check:
        existing = OUTPUT.read_text() if OUTPUT.exists() else ""
        if existing != serialized:
            print(
                f"error: {OUTPUT.relative_to(OUTPUT.parent.parent)} is out of date. "
                f"Run scripts/gen_cli_surface.py and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.write_text(serialized)
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parent.parent)} ({len(surface['commands'])} commands)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
