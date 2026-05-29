"""Unit tests for decorators module.

Tests cover @clitask decorator functionality.
"""

from unittest.mock import patch

import pytest
import typer
from plgt.core.decorators import clitask
from plgt.core.exceptions import ValidationError


class TestClitaskDecorator:
    """Test @clitask decorator functionality."""

    @patch("plgt.core.decorators.Live")
    def test_decorator_executes_function(self, mock_live):
        """Test that decorator executes the wrapped function."""

        @clitask(action="Testing")
        def test_func():
            return "success"

        result = test_func()

        assert result == "success"

    @patch("plgt.core.decorators.Live")
    def test_decorator_with_action_formatting(self, mock_live):
        """Test decorator formats action string."""

        @clitask(action="Processing {0}")
        def test_func(name):
            return f"processed {name}"

        result = test_func("test-item")

        assert result == "processed test-item"

    @patch("plgt.core.decorators.Live")
    def test_decorator_retry_on_failure(self, mock_live):
        """Test decorator retries on failure."""
        call_count = {"count": 0}

        @clitask(action="Testing", max_retries=2)
        def failing_func():
            call_count["count"] += 1
            if call_count["count"] < 3:
                msg = "Temporary failure"
                raise ValueError(msg)
            return "success"

        result = failing_func()

        assert result == "success"
        assert call_count["count"] == 3  # Initial + 2 retries

    @patch("plgt.core.decorators.Live")
    def test_decorator_max_retries_exceeded(self, mock_live):
        """Test decorator fails after max retries."""

        @clitask(action="Testing", max_retries=1)
        def always_fails():
            msg = "Always fails"
            raise ValueError(msg)

        with pytest.raises(typer.Exit):
            always_fails()

    @patch("plgt.core.decorators.Live")
    def test_decorator_cli_error_exit_code(self, mock_live):
        """Test decorator uses CLIError exit code."""

        @clitask(action="Testing")
        def cli_error_func():
            msg = "Validation failed"
            raise ValidationError(msg)

        with pytest.raises(typer.Exit) as exc_info:
            cli_error_func()

        assert exc_info.value.exit_code == 3  # ValidationError exit code

    @patch("plgt.core.decorators.Live")
    def test_decorator_generic_error_exit_code(self, mock_live):
        """Test decorator uses exit code 1 for generic errors."""

        @clitask(action="Testing")
        def generic_error_func():
            msg = "Generic error"
            raise ValueError(msg)

        with pytest.raises(typer.Exit) as exc_info:
            generic_error_func()

        assert exc_info.value.exit_code == 1

    @patch("plgt.core.decorators.Live")
    def test_decorator_no_retries_by_default(self, mock_live):
        """Test decorator doesn't retry by default."""
        call_count = {"count": 0}

        @clitask(action="Testing")  # No max_retries
        def failing_func():
            call_count["count"] += 1
            msg = "Fails"
            raise ValueError(msg)

        with pytest.raises(typer.Exit):
            failing_func()

        assert call_count["count"] == 1  # Only called once
