"""Shared shape validation for registry publisher slugs and package names.

Mirrors the authoritative registry naming constraint so that build-time CLI
validation matches what the platform will accept. Keeping the regex literally
identical to the server-side constant avoids the failure mode where the CLI
accepts a name that the API later rejects.
"""

from __future__ import annotations

import re

# Lowercase alphanumeric segments separated by single hyphens. No leading or
# trailing hyphen, no consecutive hyphens, no other punctuation.
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
SLUG_REGEX = re.compile(SLUG_PATTERN)

MIN_LENGTH = 2
MAX_LENGTH = 255

FORMAT_DESCRIPTION = (
    "must be lowercase alphanumeric with single-hyphen separators "
    '(e.g. "poliglot", "my-package")'
)


def validate_registry_slug(field_name: str, value: str) -> None:
    """Validate that ``value`` is a valid registry slug, raising on failure.

    Caller-facing errors use ``ValueError`` so they map cleanly to either
    ``BuildError`` (build path) or ``ValidationError`` (install path) at the
    nearest converter.
    """
    if value is None:
        msg = f"{field_name} is required"
        raise ValueError(msg)
    length = len(value)
    if length < MIN_LENGTH or length > MAX_LENGTH:
        msg = (
            f"{field_name} length must be between {MIN_LENGTH} and "
            f"{MAX_LENGTH} characters"
        )
        raise ValueError(msg)
    if not SLUG_REGEX.match(value):
        msg = f"{field_name} {FORMAT_DESCRIPTION}"
        raise ValueError(msg)
