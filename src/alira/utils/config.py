"""Read an independently owned YAML configuration file."""

from collections.abc import Mapping
from pathlib import Path

import yaml


class YamlConfig:
    def __init__(self, path: str | Path):
        path = Path(path)
        if path.exists():
            with path.open(encoding="utf-8") as file:
                data = yaml.safe_load(file)
        else:
            data = None
        if data is not None and not isinstance(data, dict):
            raise ValueError(f"Configuration must be a YAML mapping: {path}")
        self._data = data if data is not None else {}

    def get(self, path: str, default=None):
        value = self._data
        for key in path.split("."):
            if not isinstance(value, Mapping) or key not in value:
                return default
            value = value[key]
        return default if value is None else value
