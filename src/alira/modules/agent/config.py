"""Agent settings from this module's config.yaml."""

from pathlib import Path

from alira.utils.config import YamlConfig

_config = YamlConfig(Path(__file__).with_suffix(".yaml"))

GENERATION_TTL = _config.get("generation_ttl", 600)
CONTAINER_TTL = _config.get("container_ttl", 6000)
GRAPH_CACHE_TTL = _config.get("graph_cache_ttl", 600)
