"""Unit tests for async_retry decorator.

Tests cover retry behavior, exponential backoff, exception filtering,
and callback hooks.
"""

import asyncio

import pytest
from plgt.utils.retry import async_retry


class TestAsyncRetryBasicBehavior:
    """Test basic retry functionality."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        """Test that successful call returns immediately without retry."""
        call_count = 0

        @async_retry(max_attempts=3)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await succeed()

        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_success_after_failures(self):
        """Test that function retries and eventually succeeds."""
        call_count = 0

        @async_retry(max_attempts=5, initial_delay=0.01)
        async def fail_twice_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "temporary failure"
                raise ValueError(msg)
            return "success"

        result = await fail_twice_then_succeed()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_all_attempts(self):
        """Test that all attempts are used before raising."""
        call_count = 0

        @async_retry(max_attempts=3, initial_delay=0.01)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            msg = "persistent failure"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="persistent failure"):
            await always_fail()

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self):
        """Test that decorated function preserves name and docstring."""

        @async_retry()
        async def my_function():
            """My docstring."""
            return 42

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


class TestExponentialBackoff:
    """Test exponential backoff timing."""

    @pytest.mark.asyncio
    async def test_delays_increase_exponentially(self):
        """Test that delays follow exponential backoff pattern."""
        call_count = 0

        @async_retry(
            max_attempts=4,
            initial_delay=0.1,
            backoff_multiplier=2.0,
            max_delay=10.0,
        )
        async def track_timing():
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                msg = "fail"
                raise ValueError(msg)
            return "done"

        start = asyncio.get_event_loop().time()
        await track_timing()
        total_time = asyncio.get_event_loop().time() - start

        # Expected delays: 0.1, 0.2, 0.4 = 0.7 total (with some tolerance)
        assert 0.6 < total_time < 1.0

    @pytest.mark.asyncio
    async def test_delay_capped_at_max_delay(self):
        """Test that delay never exceeds max_delay."""
        call_count = 0

        @async_retry(
            max_attempts=5,
            initial_delay=0.1,
            backoff_multiplier=10.0,  # Would quickly exceed max
            max_delay=0.15,
        )
        async def capped_delay():
            nonlocal call_count
            call_count += 1
            if call_count < 5:
                msg = "fail"
                raise ValueError(msg)
            return "done"

        start = asyncio.get_event_loop().time()
        await capped_delay()
        total_time = asyncio.get_event_loop().time() - start

        # 4 delays, each capped at 0.15 max = 0.6 max (first is 0.1)
        # Actual: 0.1 + 0.15 + 0.15 + 0.15 = 0.55
        assert total_time < 0.8


class TestRetryableExceptions:
    """Test exception filtering for retry decisions."""

    @pytest.mark.asyncio
    async def test_retries_specified_exceptions(self):
        """Test that specified exceptions trigger retry."""
        call_count = 0

        @async_retry(
            max_attempts=3,
            initial_delay=0.01,
            retryable_exceptions=(ValueError,),
        )
        async def fail_with_value_error():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "retryable"
                raise ValueError(msg)
            return "success"

        result = await fail_with_value_error()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_unspecified_exceptions(self):
        """Test that unspecified exceptions are raised immediately."""
        call_count = 0

        @async_retry(
            max_attempts=5,
            initial_delay=0.01,
            retryable_exceptions=(ValueError,),
        )
        async def fail_with_type_error():
            nonlocal call_count
            call_count += 1
            msg = "not retryable"
            raise TypeError(msg)

        with pytest.raises(TypeError, match="not retryable"):
            await fail_with_type_error()

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_retries_multiple_exception_types(self):
        """Test that multiple exception types can be retried."""
        call_count = 0

        @async_retry(
            max_attempts=4,
            initial_delay=0.01,
            retryable_exceptions=(ValueError, TypeError, KeyError),
        )
        async def fail_with_different_errors():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "first"
                raise ValueError(msg)
            if call_count == 2:
                msg = "second"
                raise TypeError(msg)
            if call_count == 3:
                msg = "third"
                raise KeyError(msg)
            return "success"

        result = await fail_with_different_errors()

        assert result == "success"
        assert call_count == 4


class TestShouldRetryPredicate:
    """Test custom should_retry predicate."""

    @pytest.mark.asyncio
    async def test_should_retry_predicate_allows_retry(self):
        """Test that should_retry=True triggers retry."""
        call_count = 0

        @async_retry(
            max_attempts=3,
            initial_delay=0.01,
            should_retry=lambda e: "temporary" in str(e),
        )
        async def conditional_fail():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "temporary failure"
                raise ValueError(msg)
            return "success"

        result = await conditional_fail()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_should_retry_predicate_prevents_retry(self):
        """Test that should_retry=False prevents retry."""
        call_count = 0

        @async_retry(
            max_attempts=5,
            initial_delay=0.01,
            should_retry=lambda e: "temporary" in str(e),
        )
        async def permanent_fail():
            nonlocal call_count
            call_count += 1
            msg = "permanent failure"
            raise ValueError(msg)  # "temporary" not in message

        with pytest.raises(ValueError, match="permanent"):
            await permanent_fail()

        assert call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_should_retry_takes_precedence_over_retryable_exceptions(self):
        """Test that should_retry overrides retryable_exceptions."""
        call_count = 0

        @async_retry(
            max_attempts=5,
            initial_delay=0.01,
            retryable_exceptions=(ValueError,),  # Would normally retry
            should_retry=lambda e: False,  # But predicate says no
        )
        async def no_retry():
            nonlocal call_count
            call_count += 1
            msg = "should not retry"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="should not retry"):
            await no_retry()

        assert call_count == 1  # should_retry=False prevents retry


class TestOnRetryCallback:
    """Test on_retry callback functionality."""

    @pytest.mark.asyncio
    async def test_sync_on_retry_callback_called(self):
        """Test that sync on_retry callback is invoked."""
        retry_log = []

        def log_retry(exc, attempt):
            retry_log.append((str(exc), attempt))

        call_count = 0

        @async_retry(max_attempts=3, initial_delay=0.01, on_retry=log_retry)
        async def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = f"error {call_count}"
                raise ValueError(msg)
            return "success"

        await fail_twice()

        assert len(retry_log) == 2
        assert retry_log[0] == ("error 1", 1)
        assert retry_log[1] == ("error 2", 2)

    @pytest.mark.asyncio
    async def test_async_on_retry_callback_awaited(self):
        """Test that async on_retry callback is properly awaited."""
        cleanup_count = 0

        async def async_cleanup(exc, attempt):
            nonlocal cleanup_count
            await asyncio.sleep(0.01)  # Simulate async work
            cleanup_count += 1

        call_count = 0

        @async_retry(max_attempts=3, initial_delay=0.01, on_retry=async_cleanup)
        async def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "error"
                raise ValueError(msg)
            return "success"

        await fail_twice()

        assert cleanup_count == 2

    @pytest.mark.asyncio
    async def test_on_retry_receives_correct_attempt_number(self):
        """Test that on_retry receives sequential attempt numbers."""
        attempts = []

        def track_attempt(exc, attempt):
            attempts.append(attempt)

        @async_retry(max_attempts=4, initial_delay=0.01, on_retry=track_attempt)
        async def always_fail():
            msg = "fail"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="fail"):
            await always_fail()

        # on_retry called for attempts 1, 2, 3 (not 4, as that's the last)
        assert attempts == [1, 2, 3]


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_max_attempts_one_no_retry(self):
        """Test that max_attempts=1 means no retries."""
        call_count = 0

        @async_retry(max_attempts=1)
        async def single_attempt():
            nonlocal call_count
            call_count += 1
            msg = "fail"
            raise ValueError(msg)

        with pytest.raises(ValueError, match="fail"):
            await single_attempt()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_returns_correct_value_type(self):
        """Test that return value type is preserved."""

        @async_retry(max_attempts=2, initial_delay=0.01)
        async def return_dict():
            return {"key": "value", "count": 42}

        result = await return_dict()

        assert result == {"key": "value", "count": 42}
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_handles_none_return(self):
        """Test that None return value works correctly."""

        @async_retry(max_attempts=2)
        async def return_none():
            return None

        result = await return_none()

        assert result is None

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        """Test that arguments are correctly passed through."""

        @async_retry(max_attempts=2)
        async def with_args(a, b, c=None):
            return f"{a}-{b}-{c}"

        result = await with_args("x", "y", c="z")

        assert result == "x-y-z"

    @pytest.mark.asyncio
    async def test_zero_initial_delay(self):
        """Test that zero initial delay works."""
        call_count = 0

        @async_retry(max_attempts=3, initial_delay=0)
        async def quick_retry():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "fail"
                raise ValueError(msg)
            return "success"

        start = asyncio.get_event_loop().time()
        result = await quick_retry()
        elapsed = asyncio.get_event_loop().time() - start

        assert result == "success"
        assert elapsed < 0.1  # Should be nearly instant
