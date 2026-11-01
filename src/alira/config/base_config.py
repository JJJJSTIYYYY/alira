"""Alira application settings backed by Apixis configuration."""

import os

from apixis.core.config.base import _get_config

# Server
ALIRA_BASE_DIR = _get_config("SERVER.base_dir", "./.alira_data/")
BASE_URL = _get_config("SERVER.base_url", "http://localhost:2712")

# Log
DEBUG_LEVEL = _get_config("LOG.debug_level", "DEBUG")
TRACE = _get_config("LOG.trace", True)
MAX_LOG_FILE_SIZE = _get_config("LOG.max_log_file_size", 5242880)
LOG_BUFFER_SIZE = _get_config("LOG.log_buffer_size", 100)

# Runtime
GENERATION_TTL = _get_config("RUNTIME.generation_ttl", 600)
CONTAINER_TTL = _get_config("RUNTIME.container_ttl", 6000)
GRAPH_CACHE_TTL = _get_config("RUNTIME.graph_cache_ttl", 600)
CACHE_CLEAN_INTERVAL = _get_config("RUNTIME.cache_clean_interval", 300)

# Cache
HOT_CACHE_DEFAULT_EXPIRE_SECONDS = _get_config(
    "DATACACHE.hot_cache_default_expire_seconds",
    600,
)
STATIC_CACHE_DEFAULT_EXPIRE_SECONDS = _get_config(
    "DATACACHE.static_cache_default_expire_seconds",
    604800,
)

# Database
SQLITE_DATABASE = _get_config(
    "DATABASE.sqlite.database",
    os.path.join(ALIRA_BASE_DIR, "sqlite", "alira.sqlite3"),
)
WORKER_COUNT = _get_config("DATABASE.worker_count", 4)
