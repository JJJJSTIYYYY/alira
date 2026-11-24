"""Application-wide settings from the working directory's config.yaml."""

from pathlib import Path

from alira.utils.config import YamlConfig

_config = YamlConfig("config.yaml")

# Business modules own how they interpret this data-directory setting.
ALIRA_BASE_DIR = _config.get("SERVER.base_dir", str(Path.home() / ".alira"))

DEBUG_LEVEL = _config.get("LOG.debug_level", "DEBUG")
TRACE = _config.get("LOG.trace", True)
MAX_LOG_FILE_SIZE = _config.get("LOG.max_log_file_size", 5242880)
LOG_BUFFER_SIZE = _config.get("LOG.log_buffer_size", 1024)

CACHE_CLEAN_INTERVAL = _config.get("RUNTIME.cache_clean_interval", 300)
