"""Application-wide settings from the working directory's config.yaml."""

from pathlib import Path

from alira.utils.config import YamlConfig

_config = YamlConfig("config.yaml")

_base_dir = Path(_config.get("SERVER.base_dir", Path.home() / ".alira")).expanduser()
ALIRA_BASE_DIR = str(
    (_base_dir if _base_dir.is_absolute() else Path.home() / _base_dir).resolve()
)

DEBUG_LEVEL = _config.get("LOG.debug_level", "DEBUG")
TRACE = _config.get("LOG.trace", True)
MAX_LOG_FILE_SIZE = _config.get("LOG.max_log_file_size", 5242880)
LOG_BUFFER_SIZE = _config.get("LOG.log_buffer_size", 1024)

CACHE_CLEAN_INTERVAL = _config.get("RUNTIME.cache_clean_interval", 300)
