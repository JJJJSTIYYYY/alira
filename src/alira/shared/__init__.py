"""Application-wide configuration, logging and version support."""

from alira.shared.application import config
from alira.shared.application.config import (
    ALIRA_BASE_DIR,
    CACHE_CLEAN_INTERVAL,
    DEBUG_LEVEL,
    LOG_BUFFER_SIZE,
    MAX_LOG_FILE_SIZE,
    TRACE,
)
from alira.shared.application.version import compare_version, print_logo
from alira.shared.logger import (
    AliraLogHandler,
    Logger,
    LogWriter,
    logger,
    register_log_level,
    unregister_log_level,
)
from alira.shared.type import ModuleNotLoaded, ModuleNotLoadedError

__all__ = [
    "ALIRA_BASE_DIR",
    "CACHE_CLEAN_INTERVAL",
    "DEBUG_LEVEL",
    "LOG_BUFFER_SIZE",
    "MAX_LOG_FILE_SIZE",
    "TRACE",
    "AliraLogHandler",
    "LogWriter",
    "Logger",
    "ModuleNotLoaded",
    "ModuleNotLoadedError",
    "compare_version",
    "config",
    "logger",
    "print_logo",
    "register_log_level",
    "unregister_log_level",
]
