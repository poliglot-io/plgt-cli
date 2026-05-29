"""Unit tests for the version range utility.

Includes parametrized parity tests that load
``plgt/utils/test_data/version_range_parity.json`` and assert
identical behaviour to the authoritative server-side evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plgt.utils.version_range import (
    extract_major_version,
    is_upgrade,
    satisfies_range,
    validate_range,
)

_PARITY_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "plgt"
    / "utils"
    / "test_data"
    / "version_range_parity.json"
)


def _load_fixtures() -> dict:
    with _PARITY_FIXTURES.open() as f:
        return json.load(f)


class TestSatisfiesRange:
    def test_workspace_within_two_bound_range_is_accepted(self):
        assert satisfies_range("2.1.5", ">=2.1 <3.0") is True

    def test_workspace_below_floor_is_rejected(self):
        assert satisfies_range("2.0.7", ">=2.1 <4") is False

    def test_workspace_at_exclusive_ceiling_is_rejected(self):
        assert satisfies_range("3.0.0", ">=2 <3") is False

    def test_workspace_just_below_ceiling_patch_ignored(self):
        assert satisfies_range("2.9.99", ">=2 <3") is True

    def test_single_bound_floor_accepts_at_floor(self):
        assert satisfies_range("2.0.0", ">=2") is True

    def test_single_bound_floor_rejects_below_floor(self):
        assert satisfies_range("1.99.99", ">=2") is False

    def test_single_bound_ceiling_accepts_below_ceiling(self):
        assert satisfies_range("2.5.3", "<3") is True

    def test_single_bound_ceiling_rejects_at_ceiling(self):
        assert satisfies_range("3.0.0", "<3") is False

    def test_bare_major_and_major_minor_evaluate_identically(self):
        assert satisfies_range("3.0.0", "<3") == satisfies_range("3.0.0", "<3.0")

    def test_exact_minor_match_in_two_bound_range(self):
        assert satisfies_range("2.1.4", ">=2.1 <3") is True


class TestExtractMajorVersion:
    def test_typical_semver(self):
        assert extract_major_version("2.1.5") == "2"

    def test_zero_major(self):
        assert extract_major_version("0.1.0") == "0"

    def test_multi_digit_major(self):
        assert extract_major_version("100.50.0") == "100"

    def test_invalid_version_raises(self):
        with pytest.raises(ValueError, match=r".+"):
            extract_major_version("")
        with pytest.raises(ValueError, match=r".+"):
            extract_major_version("nope")


class TestIsUpgrade:
    def test_candidate_greater_returns_true(self):
        assert is_upgrade("2.1.5", "2.1.6") is True
        assert is_upgrade("2.1.5", "2.2.0") is True
        assert is_upgrade("2.1.5", "3.0.0") is True

    def test_candidate_less_returns_false(self):
        assert is_upgrade("2.1.5", "2.1.4") is False
        assert is_upgrade("2.1.5", "2.0.99") is False
        assert is_upgrade("2.1.5", "1.99.99") is False

    def test_candidate_equal_returns_false(self):
        assert is_upgrade("2.1.5", "2.1.5") is False


class TestValidateRange:
    @pytest.mark.parametrize(
        "good",
        [
            ">=2",
            ">=2.1",
            "<3",
            "<3.0",
            ">=2.1 <3.0",
            ">=2 <4",
        ],
    )
    def test_accepted_forms_do_not_raise(self, good: str):
        validate_range(good)

    def test_patch_component_rejected_with_informative_message(self):
        with pytest.raises(ValueError, match="patch"):
            validate_range(">=2.1.3")

    def test_patch_component_in_second_bound_rejected(self):
        with pytest.raises(ValueError, match="patch"):
            validate_range(">=2 <2.1.5")

    @pytest.mark.parametrize(
        "bad",
        [
            ">2.1",
            "<=3.0",
            "=2.1",
            "~2.1",
            "^2.1",
        ],
    )
    def test_unsupported_operators_rejected(self, bad: str):
        with pytest.raises(ValueError, match=r".+"):
            validate_range(bad)

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_and_whitespace_rejected(self, bad: str):
        with pytest.raises(ValueError, match=r".+"):
            validate_range(bad)

    @pytest.mark.parametrize(
        "bad",
        [
            ">=",
            "2.1",
            ">=2.1 <3.0 <4.0",
            ">=abc",
        ],
    )
    def test_malformed_structure_rejected(self, bad: str):
        with pytest.raises(ValueError, match=r".+"):
            validate_range(bad)

    def test_none_rejected(self):
        with pytest.raises(ValueError, match=r".+"):
            validate_range(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad",
        [
            ">=2 >=3",
            "<3 <5",
        ],
    )
    def test_two_same_op_bounds_rejected(self, bad: str):
        # ">=2 >=3" silently shadows to ">=3" if accepted; surface the authoring error.
        with pytest.raises(ValueError, match="same operator"):
            validate_range(bad)

    @pytest.mark.parametrize(
        "bad",
        [
            ">=3 <3",
            ">=5 <3",
            ">=2.5 <2.3",
        ],
    )
    def test_empty_or_inverted_interval_rejected(self, bad: str):
        with pytest.raises(ValueError, match="empty"):
            validate_range(bad)


# --- parity fixtures --------------------------------------------------------

_FIXTURES = _load_fixtures()


@pytest.mark.parametrize(
    "case",
    _FIXTURES["satisfies_range"],
    ids=lambda c: f"{c['workspace']} vs {c['range']} -> {c['expected']}",
)
def test_parity_satisfies_range(case: dict):
    actual = satisfies_range(case["workspace"], case["range"])
    assert actual is case["expected"], (
        f"Parity drift: satisfies_range({case['workspace']!r}, {case['range']!r}) "
        f"returned {actual}, expected {case['expected']} ({case.get('comment', '')})"
    )


@pytest.mark.parametrize("range_str", _FIXTURES["validate_range_valid"])
def test_parity_validate_range_valid(range_str: str):
    validate_range(range_str)


@pytest.mark.parametrize(
    "case",
    _FIXTURES["validate_range_invalid"],
    ids=lambda c: f"{c['input']!r} ({c['reason']})",
)
def test_parity_validate_range_invalid(case: dict):
    with pytest.raises(ValueError, match=r".+"):
        validate_range(case["input"])
