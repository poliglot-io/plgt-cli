"""Unit tests for SHACL validation report display.

Tests cover formatting and display of validation entries.
"""

from io import StringIO

from plgt.models.lifecycle_command import ValidationEntry, ValidationReport
from plgt.services.validation_report import (
    _format_info,
    _format_violation,
    _format_warning,
    display_validation_report,
)
from rich.console import Console


class TestFormatViolation:
    """Test _format_violation function."""

    def test_formats_complete_violation(self):
        """Test formatting of violation with all fields."""
        entry = ValidationEntry(
            focus_node="ex:Person1",
            path="ex:name",
            message="Name is required",
            value="invalid",
        )

        result = _format_violation(entry)

        assert "ex:Person1" in result
        assert "ex:name" in result
        assert "invalid" in result
        assert "Name is required" in result

    def test_formats_violation_without_node(self):
        """Test formatting when node is missing."""
        entry = ValidationEntry(
            focus_node=None,
            path="ex:name",
            message="Name is required",
            value="invalid",
        )

        result = _format_violation(entry)

        assert "Resource" not in result  # No node prefix
        assert "ex:name" in result

    def test_formats_violation_without_value(self):
        """Test formatting when value is missing."""
        entry = ValidationEntry(
            focus_node="ex:Person1",
            path="ex:name",
            message="Required field",
            value=None,
        )

        result = _format_violation(entry)

        assert "missing or empty" in result

    def test_formats_violation_without_path(self):
        """Test formatting when path is missing."""
        entry = ValidationEntry(
            focus_node="ex:Person1",
            path=None,
            message="Error",
            value="bad",
        )

        result = _format_violation(entry)

        assert "[unknown property]" in result

    def test_handles_empty_node_string(self):
        """Test handling of empty node string."""
        entry = ValidationEntry(
            focus_node="",
            path="ex:prop",
            message="Error",
            value="val",
        )

        result = _format_violation(entry)

        assert "Resource" not in result

    def test_handles_none_message(self):
        """Test handling of None message."""
        entry = ValidationEntry(
            focus_node="ex:Node",
            path="ex:prop",
            message=None,
            value="val",
        )

        result = _format_violation(entry)

        assert "Constraint violation" in result


class TestFormatWarning:
    """Test _format_warning function."""

    def test_formats_complete_warning(self):
        """Test formatting of warning with all fields."""
        entry = ValidationEntry(
            focus_node="ex:Person1",
            path="ex:age",
            message="Age should be positive",
            value="-5",
        )

        result = _format_warning(entry)

        assert "ex:Person1" in result
        assert "ex:age" in result
        assert "-5" in result
        assert "Age should be positive" in result

    def test_formats_warning_without_value(self):
        """Test formatting when value is missing."""
        entry = ValidationEntry(
            focus_node="ex:Person1",
            path="ex:status",
            message="Status recommended",
            value=None,
        )

        result = _format_warning(entry)

        assert "ex:status" in result
        assert "Status recommended" in result


class TestFormatInfo:
    """Test _format_info function."""

    def test_formats_complete_info(self):
        """Test formatting of info with all fields."""
        entry = ValidationEntry(
            focus_node="ex:Resource",
            path="ex:optional",
            message="Optional field not set",
            value=None,
        )

        result = _format_info(entry)

        assert "ex:Resource" in result
        assert "ex:optional" in result
        assert "Optional field not set" in result

    def test_formats_info_without_path(self):
        """Test formatting when path is missing."""
        entry = ValidationEntry(
            focus_node="ex:Resource",
            path=None,
            message="General info",
            value=None,
        )

        result = _format_info(entry)

        assert "ex:Resource" in result
        assert "General info" in result


class TestDisplayValidationReport:
    """Test display_validation_report function."""

    def test_displays_violations_and_warnings(self):
        """Test display of complete validation report."""
        report = ValidationReport(
            conforms=False,
            violation_count=1,
            warning_count=1,
            info_count=0,
            violations=[
                ValidationEntry(
                    focus_node="ex:A",
                    path="ex:prop",
                    message="Violation message",
                    value=None,
                )
            ],
            warnings=[
                ValidationEntry(
                    focus_node="ex:B",
                    path="ex:prop2",
                    message="Warning message",
                    value="val",
                )
            ],
            infos=[],
        )

        output = StringIO()
        console = Console(file=output, force_terminal=True)

        display_validation_report(report, console)

        result = output.getvalue()
        assert "1" in result and "violation" in result.lower()
        assert "1" in result and "warning" in result.lower()

    def test_displays_infos(self):
        """Test display of info messages."""
        report = ValidationReport(
            conforms=True,
            violation_count=0,
            warning_count=0,
            info_count=2,
            violations=[],
            warnings=[],
            infos=[
                ValidationEntry(
                    focus_node="ex:A",
                    path=None,
                    message="Info 1",
                    value=None,
                ),
                ValidationEntry(
                    focus_node="ex:B",
                    path=None,
                    message="Info 2",
                    value=None,
                ),
            ],
        )

        output = StringIO()
        console = Console(file=output, force_terminal=True)

        display_validation_report(report, console)

        result = output.getvalue()
        assert "2" in result and "info" in result.lower()

    def test_displays_empty_report(self):
        """Test display of empty validation report."""
        report = ValidationReport(
            conforms=True,
            violation_count=0,
            warning_count=0,
            info_count=0,
            violations=[],
            warnings=[],
            infos=[],
        )

        output = StringIO()
        console = Console(file=output, force_terminal=True)

        display_validation_report(report, console)

        result = output.getvalue()
        # Empty report produces no output
        assert result == ""

    def test_displays_violation_details(self):
        """Test that violation details are displayed."""
        report = ValidationReport(
            conforms=False,
            violation_count=1,
            warning_count=0,
            info_count=0,
            violations=[
                ValidationEntry(
                    focus_node="ex:TestNode",
                    path="ex:testProperty",
                    message="Specific error message",
                    value="badvalue",
                )
            ],
            warnings=[],
            infos=[],
        )

        output = StringIO()
        console = Console(file=output, force_terminal=True)

        display_validation_report(report, console)

        result = output.getvalue()
        assert "1" in result and "violation" in result.lower()
        assert "Specific error message" in result
