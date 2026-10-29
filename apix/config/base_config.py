"""Apix application settings backed by Apixis configuration."""

import os

from apixis.core.config.base import _get_config

VERSION = "3.0.0"


# Server
APIX_BASE_DIR = _get_config("SERVER.base_dir", "./.apix/")
BASE_URL = _get_config("SERVER.base_url", "http://localhost:2712")
WORKER_COUNT = _get_config("SERVER.worker_count", 4)


_PROVIDER_BASE_URL = {
    "ollama:local": "http://localhost:11434",
    "ollama": "https://ollama.com",
    "openai": "https://api.openai.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "xiaomimimo": "https://api.xiaomimimo.com/v1",
    "minimax": "https://api.minimaxi.com/v1"
}

# Runtime
TOOLS_MAX_OUTPUT_LENGTH = _get_config(
    "RUNTIME.tools_max_output_length",
    32000,
)
MAX_RETRY = _get_config("RUNTIME.max_retry", 8)
GENERATION_TTL = _get_config("RUNTIME.generation_ttl", 600)
CONTAINER_TTL = _get_config("RUNTIME.container_ttl", 6000)
GRAPH_CACHE_TTL = _get_config("RUNTIME.graph_cache_ttl", 600)
CACHE_CLEAN_INTERVAL = _get_config("RUNTIME.cache_clean_interval", 300)


# Cache
HOT_CACHE_DEFAULT_EXPIRE_SECONDS = _get_config(
    "CACHE.hot_cache_default_expire_seconds",
    600,
)
STATIC_CACHE_DEFAULT_EXPIRE_SECONDS = _get_config(
    "CACHE.static_cache_default_expire_seconds",
    604800,
)


# Data store
SQLITE_DATABASE = _get_config(
    "DATA_STORE.sqlite.database",
    os.path.join(APIX_BASE_DIR, "sqlite", "apix.sqlite3"),
)


# LLM
PROVIDER_BASE_URL = _get_config(
    "LLM.provider_base_url",
    _PROVIDER_BASE_URL,
)
LLM_MAX_RETRY = _get_config("LLM.max_retry", 3)
LLM_TIMEOUT = _get_config("LLM.timeout", 30)
