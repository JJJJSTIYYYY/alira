"""Storage and cache settings from this module's config.yaml."""

import os
from pathlib import Path

from alira.shared.application.config import ALIRA_BASE_DIR
from alira.utils.config import YamlConfig

_config = YamlConfig(Path(__file__).with_suffix(".yaml"))

SQLITE_DATABASE = _config.get(
    "sqlite.database", os.path.join(ALIRA_BASE_DIR, "sqlite", "alira.sqlite3")
)
SQLITE_EMBEDDING_MODEL = _config.get("sqlite.embedding_model", "")
SQLITE_EMBEDDING_ENABLE = _config.get("sqlite.embedding_enable", False)
SQLITE_EMBEDDING_OPTIONS = _config.get("sqlite.embedding_options", {})
VECTOR_DIMENSION = _config.get("sqlite.vector_dimension", 640)
WORKER_COUNT = _config.get("worker_count", 4)

HOT_CACHE_DEFAULT_EXPIRE_SECONDS = _config.get(
    "cache.hot_cache_default_expire_seconds", 600
)
STATIC_CACHE_DEFAULT_EXPIRE_SECONDS = _config.get(
    "cache.static_cache_default_expire_seconds", 604800
)
