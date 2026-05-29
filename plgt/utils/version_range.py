"""Engine version and compatibility-range utilities.

Workspaces store a full semver (e.g. ``2.1.5``) for their installed engine.
Packages declare engine compatibility as a range at major.minor granularity
(e.g. ``>=2.1 <3.0``). Patch components are intentionally forbidden inside
ranges: workspaces are always brought to the latest patch of their current
minor by auto-patch, so patch is not a meaningful axis for declaring
compatibility.

Range syntax accepted:

* Operators: ``>=``, ``<`` only.
* Components: 1 (bare major) or 2 (major.minor). 3-component (with patch)
  is rejected.
* Single bound: ``>=2``, ``>=2.1``, ``<3``, ``<3.0``.
* Two bounds (logical AND, space-separated): ``>=2.1 <3.0``, ``>=2 <4``.

The range parser is custom (not delegated to the ``semver`` library's range
support) because the library's range modes do not match these exact rules,
and we evaluate against major.minor only after stripping the workspace
patch component.

This module must remain in lockstep with the authoritative engine-version
evaluator. Drift is regressed against by the shared parity fixtures at
``plgt/utils/test_data/version_range_parity.json``.
"""

from __future__ import annotations

from dataclasses import dataclass

import semver

_ALLOWED_OPS = (">=", "<")


@dataclass(frozen=True)
class _Bound:
    op: str  # ">=" or "<"
    major: int
    minor: int

    def matches(self, ws_major: int, ws_minor: int) -> bool:
        ws = (ws_major, ws_minor)
        ref = (self.major, self.minor)
        if self.op == ">=":
            return ws >= ref
        return ws < ref


def satisfies_range(workspace_version: str, package_range: str) -> bool:
    """Return True iff ``workspace_version`` satisfies ``package_range``.

    Only the major.minor of the workspace participates; the patch component
    is ignored.

    Raises ``ValueError`` if either input is malformed.
    """
    if workspace_version is None:
        msg = "Workspace version is required"
        raise ValueError(msg)
    ws = _parse_semver(workspace_version)
    bounds = _parse_range(package_range)
    return all(b.matches(ws.major, ws.minor) for b in bounds)


def extract_major_version(version: str) -> str:
    """Return the major component of a full semver as a string."""
    if version is None:
        msg = "Version is required"
        raise ValueError(msg)
    return str(_parse_semver(version).major)


def is_upgrade(current_version: str, candidate_version: str) -> bool:
    """Return True iff candidate is strictly greater than current under semver order."""
    current = _parse_semver(current_version)
    candidate = _parse_semver(candidate_version)
    return candidate.compare(current) > 0


def validate_range(range_str: str) -> None:
    """Validate that ``range_str`` is a syntactically acceptable range.

    Raises ``ValueError`` with a clear message on failure.
    """
    _parse_range(range_str)


# --- internals --------------------------------------------------------------


def _parse_semver(version: str) -> semver.Version:
    if version is None or not version.strip():
        msg = "Version is required"
        raise ValueError(msg)
    try:
        return semver.Version.parse(version.strip())
    except ValueError as e:
        msg = f"Version '{version}' is not a valid semver: {e}"
        raise ValueError(msg) from e


def _parse_range(range_str: str | None) -> list[_Bound]:
    if range_str is None:
        msg = "Range is required"
        raise ValueError(msg)
    trimmed = range_str.strip()
    if not trimmed:
        msg = f"Range '{range_str}' is empty"
        raise ValueError(msg)
    parts = trimmed.split()
    if not 1 <= len(parts) <= 2:
        msg = (
            f"Range '{range_str}' must contain one or two space-separated bounds "
            f"(got {len(parts)})"
        )
        raise ValueError(msg)
    bounds = [_parse_bound(p, range_str) for p in parts]
    if len(bounds) == 2:
        # A two-bound range must combine one lower bound (>=) with one upper bound (<).
        # Two bounds with the same operator silently shadow each other in interval terms,
        # which is a sign of authoring confusion. Reject up-front so the failure surfaces
        # at parse time rather than as silent "no version matched" downstream.
        if bounds[0].op == bounds[1].op:
            msg = (
                f"Range '{range_str}' uses the same operator '{bounds[0].op}' on both "
                f"bounds; combine '>=' with '<'"
            )
            raise ValueError(msg)
        # Reject empty intervals (lower >= upper). ">=3 <3" admits no major.minor point;
        # ">=5 <3" is inverted. Both are author errors that should fail loud.
        lower = bounds[0] if bounds[0].op == ">=" else bounds[1]
        upper = bounds[0] if bounds[0].op == "<" else bounds[1]
        if (lower.major, lower.minor) >= (upper.major, upper.minor):
            msg = (
                f"Range '{range_str}' is empty: lower bound "
                f"'{lower.op}{lower.major}.{lower.minor}' is not less than upper bound "
                f"'{upper.op}{upper.major}.{upper.minor}'"
            )
            raise ValueError(msg)
    return bounds


def _parse_bound(token: str, full_range: str) -> _Bound:
    # Order matters: ">=" before ">", "<=" before "<".
    if token.startswith(">="):
        op = ">="
        version_part = token[2:]
    elif token.startswith("<="):
        msg = (
            f"Range '{full_range}' uses unsupported operator '<='; "
            f"only '>=' and '<' are permitted"
        )
        raise ValueError(msg)
    elif token.startswith("<"):
        op = "<"
        version_part = token[1:]
    elif token[:1] in (">", "=", "~", "^"):
        msg = (
            f"Range '{full_range}' uses unsupported operator in bound '{token}'; "
            f"only '>=' and '<' are permitted"
        )
        raise ValueError(msg)
    else:
        msg = f"Range '{full_range}' bound '{token}' is missing an operator"
        raise ValueError(msg)

    if op not in _ALLOWED_OPS:  # defensive: should be unreachable
        msg = f"Range '{full_range}' bound '{token}' uses unsupported operator '{op}'"
        raise ValueError(msg)

    if not version_part:
        msg = f"Range '{full_range}' bound '{token}' has no version after the operator"
        raise ValueError(msg)

    components = version_part.split(".")
    if len(components) > 2:
        msg = (
            f"Range '{full_range}' contains a patch component in bound '{token}'; "
            f"only major.minor are permitted"
        )
        raise ValueError(msg)
    major = _parse_component(components[0], token, full_range)
    minor = (
        _parse_component(components[1], token, full_range)
        if len(components) == 2
        else 0
    )
    return _Bound(op=op, major=major, minor=minor)


def _parse_component(component: str, token: str, full_range: str) -> int:
    if not component:
        msg = f"Range '{full_range}' bound '{token}' has an empty version component"
        raise ValueError(msg)
    if not component.isdigit():
        msg = (
            f"Range '{full_range}' bound '{token}' has a non-numeric "
            f"version component '{component}'"
        )
        raise ValueError(msg)
    return int(component)
