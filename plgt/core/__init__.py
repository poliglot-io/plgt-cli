import logging

from .logs import CLILogger

# configure logger class before importing submodules
logging.setLoggerClass(CLILogger)

from ._cache import DependencyCache  # noqa: E402
from ._config import AppConfig  # noqa: E402

config = AppConfig()
cache = DependencyCache()
