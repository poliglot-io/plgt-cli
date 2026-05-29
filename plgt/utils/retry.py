"""Retry utilities for async operations with exponential backoff."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def async_retry(
    max_attempts: int = 5,
    initial_delay: float = 0.5,
    max_delay: float = 5.0,
    backoff_multiplier: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
    should_retry: Callable[[Exception], bool] | None = None,
    on_retry: Callable[[Exception, int], Awaitable[None] | None] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Async retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (default 5)
        initial_delay: Initial delay in seconds (default 0.5)
        max_delay: Maximum delay cap (default 5.0)
        backoff_multiplier: Delay multiplier per attempt (default 2.0)
        retryable_exceptions: Exception types to retry on (default: all exceptions)
        should_retry: Optional predicate(exception) -> bool for custom retry logic.
            If provided, takes precedence over retryable_exceptions.
        on_retry: Optional async callback(exception, attempt_number) before retry.
            Can be used for cleanup/reset operations between retries.

    Returns:
        Decorated async function with retry behavior

    Example:
        @async_retry(max_attempts=3, initial_delay=1.0)
        async def fetch_data():
            return await api.get_data()

        @async_retry(should_retry=lambda e: isinstance(e, ConnectionError))
        async def api_call():
            return await client.call(request)
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            delay = initial_delay
            last_error: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_error = e

                    # Determine if this exception is retryable
                    is_retryable = True
                    if should_retry is not None:
                        is_retryable = should_retry(e)
                    elif retryable_exceptions is not None:
                        is_retryable = isinstance(e, retryable_exceptions)

                    # Don't retry if not retryable or out of attempts
                    if not is_retryable or attempt >= max_attempts:
                        raise

                    logger.debug(
                        "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                        func.__name__,
                        attempt,
                        max_attempts,
                        delay,
                        e,
                    )

                    # Call on_retry hook if provided (for cleanup/reset)
                    if on_retry is not None:
                        result = on_retry(e, attempt)
                        if asyncio.iscoroutine(result):
                            await result

                    await asyncio.sleep(delay)
                    delay = min(delay * backoff_multiplier, max_delay)

            # Should not reach here, but for type safety
            if last_error is not None:
                raise last_error
            msg = "Retry exhausted without error"
            raise RuntimeError(msg)

        return wrapper

    return decorator
