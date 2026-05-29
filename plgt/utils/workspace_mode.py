"""Workspace-sync vs registry-resolve mode selection for `plgt sync`,
`plgt validate`, and `plgt schema`.

The three commands share an identical precedence rule, surfaced through
``--from-workspace <slug>`` / ``--from-registry`` / the configured default
workspace (``defaults.workspace`` in the plgt config). Centralising the
rule keeps install and validate looking at the same cache subtree.
"""

from __future__ import annotations

from plgt.core import config
from plgt.core.exceptions import ValidationError


def resolve_workspace_mode(
    *,
    from_workspace: str | None,
    from_registry: bool,
) -> str | None:
    """Return the workspace slug for workspace-sync mode, or ``None`` for
    registry-resolve mode.

    Precedence: explicit ``--from-workspace <slug>`` > explicit
    ``--from-registry`` > configured default workspace > registry-resolve
    fallback (returns ``None``).

    Raises ``ValidationError`` when both ``--from-workspace`` and
    ``--from-registry`` are supplied. The caller surfaces this as an exit-1
    failure.
    """
    if from_workspace is not None and from_registry:
        msg = "--from-workspace and --from-registry are mutually exclusive; pass at most one."
        raise ValidationError(msg)
    if from_workspace is not None:
        return from_workspace
    if from_registry:
        return None
    default_workspace = config.defaults.get("workspace")
    return default_workspace if default_workspace else None


__all__ = ["resolve_workspace_mode"]
