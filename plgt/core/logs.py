import logging
import os
from logging.handlers import RotatingFileHandler

from rich.console import Console

from plgt.core import settings

# force_terminal=True ensures ANSI codes are properly rendered even when
# log messages are emitted from background threads
console = Console(force_terminal=True)

# File logging configuration
LOG_DIR = settings.CONFIG_ROOT / "logs"
LOG_FILE = LOG_DIR / "plgt.log"
MAX_BYTES = 5_000_000  # 5MB per file
BACKUP_COUNT = 3  # Keep 3 rotated files

_state: dict[str, RotatingFileHandler | None] = {"file_handler": None}


def get_file_handler() -> RotatingFileHandler:
    """Get or create the rotating file handler for persistent logging."""
    if _state["file_handler"] is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        # File always logs at DEBUG level - filtering happens at logger level
        handler.setLevel(logging.DEBUG)
        _state["file_handler"] = handler
    return _state["file_handler"]


def setup_file_logging(*, debug: bool = False) -> None:
    """
    Configure file logging for the application.

    Always logs to ~/.plgt/logs/plgt.log with rotation.
    Debug mode can also be enabled via PLGT_DEBUG=1 environment variable.

    Args:
        debug: If True, log DEBUG level to file. Otherwise INFO.
    """
    # Check env var for debug mode
    if os.environ.get("PLGT_DEBUG", "").lower() in ("1", "true", "yes"):
        debug = True

    handler = get_file_handler()
    root = logging.getLogger()

    # Add file handler if not already added
    if handler not in root.handlers:
        root.addHandler(handler)

    # Set file logging level
    file_level = logging.DEBUG if debug else logging.INFO
    handler.setLevel(file_level)


class CLILogger(logging.Logger):
    def __init__(self, name):
        super().__init__(name)

    def success(self, msg, *args, **kwargs):
        super().info(f"[green]{msg}[/green] \U0001f680", *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        msg = f"[yellow]{msg}[/yellow]"
        super().warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        msg = f"[red]{msg}[/red]"
        super().error(msg, *args, **kwargs)
