"""Tests for ``plgt.utils.naming``."""

from __future__ import annotations

import pytest
from plgt.utils.naming import (
    MAX_LENGTH,
    MIN_LENGTH,
    SLUG_PATTERN,
    SLUG_REGEX,
    validate_registry_slug,
)


class TestPattern:
    @pytest.mark.parametrize(
        "value",
        [
            "ab",  # min length boundary
            "poliglot",
            "claude",
            "my-package",
            "test-publisher",
            "plgt",
            "a1b2",
            "x" * MAX_LENGTH,  # max length boundary
        ],
    )
    def test_accepts_valid_slugs(self, value: str) -> None:
        assert SLUG_REGEX.match(value), f"expected {value!r} to match {SLUG_PATTERN}"

    # Each invalid case is paired with the *kind* of error message we
    # expect, so a regression that swaps which check fires (e.g. length
    # raised when format should have, or vice versa) gets caught instead of
    # silently passing on any non-empty error.
    @pytest.mark.parametrize(
        ("value", "expected_message"),
        [
            ("", r"between 2"),
            ("a", r"between 2"),  # below min length
            # "A" is length 1 — length check fires before the format check,
            # so the expected message is the length one.
            ("A", r"between 2"),
            ("Foo", r"lowercase alphanumeric"),
            ("with_underscore", r"lowercase alphanumeric"),
            ("with.dot", r"lowercase alphanumeric"),
            ("with space", r"lowercase alphanumeric"),
            ("-leading", r"lowercase alphanumeric"),
            ("trailing-", r"lowercase alphanumeric"),
            ("double--hyphen", r"lowercase alphanumeric"),
            ("ev/il", r"lowercase alphanumeric"),
            ("with@", r"lowercase alphanumeric"),
        ],
    )
    def test_rejects_invalid_slugs(self, value: str, expected_message: str) -> None:
        with pytest.raises(ValueError, match=expected_message):
            validate_registry_slug("test", value)


class TestValidateRegistrySlug:
    def test_accepts_valid(self) -> None:
        validate_registry_slug("publisher", "poliglot")

    def test_rejects_none(self) -> None:
        with pytest.raises(ValueError, match="required"):
            validate_registry_slug("publisher", None)  # type: ignore[arg-type]

    def test_too_short_message_mentions_length(self) -> None:
        with pytest.raises(ValueError, match=f"between {MIN_LENGTH}"):
            validate_registry_slug("publisher", "x")

    def test_invalid_format_message_mentions_field(self) -> None:
        with pytest.raises(ValueError, match=r"^publisher "):
            validate_registry_slug("publisher", "Bad_Slug")

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="between"):
            validate_registry_slug("publisher", "a" * (MAX_LENGTH + 1))
