import logging
import time
from functools import wraps

import typer
from rich.live import Live
from rich.status import Status

from plgt.core import settings
from plgt.core.exceptions import CLIError
from plgt.core.logs import console

logger = logging.getLogger(settings.APP_AUTHOR)


def clitask(
    action: str = "I don't know what I'm doing, but I'm doing something...",
    max_retries: int = 0,
):
    """Configure a CLI task which requires some feedback in the console."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0

            try:
                formatted_action = action.format(*args, **kwargs)
                task_status = Status(formatted_action, spinner="dots")

                with Live(
                    task_status,
                    console=console,
                    vertical_overflow="visible",
                    transient=True,
                ) as live:
                    while attempt <= max_retries:
                        try:
                            time.sleep(0.5)
                            result = func(*args, **kwargs)

                            live.stop()

                            logger.info("[green]\u2713[/green] %s", formatted_action)
                            return result
                        except Exception:
                            attempt += 1

                            if attempt > max_retries:
                                raise

                            task_status.update(
                                status=f"{formatted_action}, attempt {attempt + 1}",
                            )

            except Exception as e:
                logger.info("[red]\u00d7[/red] %s", formatted_action)
                logger.exception("%s", e.__class__.__name__)

                code = e.exit_code if isinstance(e, CLIError) else 1

                raise typer.Exit(code)  # noqa: B904

        return wrapper

    return decorator
