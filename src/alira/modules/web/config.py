"""Web settings from this module's config.yaml."""

from pathlib import Path

from alira.utils.config import YamlConfig

_config = YamlConfig(Path(__file__).with_suffix(".yaml"))

BASE_URL = _config.get("base_url", "http://localhost:2712")
STARTUP_TIMEOUT = _config.get("startup_timeout", 30)
SHUTDOWN_TIMEOUT = _config.get("shutdown_timeout", 30)
