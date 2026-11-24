"""Read YAML data and update mapping files through a shared read/write path."""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _read_yaml(path: Path) -> Any:
    """Read one document without imposing a public operation's error context."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _write_yaml(path: Path, data: dict) -> None:
    """Write one document, creating the destination's parents if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, allow_unicode=True)


def load_from_yaml(dir: str | Path, key: str | None = None) -> Any:
    """Load a YAML document or return one key from a YAML mapping.

    Args:
        dir: File to read as UTF-8.
        key: Optional top-level mapping key. Missing keys return None.

    Returns:
        Parsed YAML data. Missing files produce an empty dict; empty documents
        return None.
        When key is provided, the document must be a mapping.

    Raises:
        OSError: Reading the file failed.
        yaml.YAMLError: The document contains invalid YAML.
    """
    try:
        data = _read_yaml(Path(dir))
        return data if key is None else data.get(key)
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Error loading yaml file: {exc}")
        raise


def write_to_yaml(dir: str | Path, data: dict) -> None:
    """Overwrite a UTF-8 YAML file, creating its parent directories when needed.

    Args:
        dir: Destination file.
        data: Mapping to serialize, preserving Unicode characters.

    Raises:
        OSError: Creating the parent directory or writing the file failed.
        yaml.YAMLError: The mapping cannot be serialized as safe YAML.
    """
    try:
        _write_yaml(Path(dir), data)
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Error writing to yaml file: {exc}")
        raise


def append_to_yaml(dir: str | Path, new_data: dict) -> None:
    """Merge top-level mapping keys into a YAML file, replacing existing values.

    Args:
        dir: Mapping file to update; missing files start with an empty mapping.
        new_data: Keys and values to apply using dict.update.

    Raises:
        OSError: Reading or writing the file failed.
        yaml.YAMLError: YAML parsing or serialization failed.
    """
    try:
        path = Path(dir)
        data = _read_yaml(path) or {}
        data.update(new_data)
        _write_yaml(path, data)
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Error appending to yaml file: {exc}")
        raise


def update_to_yaml(file_path: Path, title: str, content: str) -> dict:
    """Set or delete a top-level mapping key and return the updated mapping.

    Args:
        file_path: Mapping file to update; missing files start with an empty mapping.
        title: Key to update or delete.
        content: Value to store. Whitespace-only content deletes the key if present.

    Returns:
        The mapping written to the file.

    Raises:
        OSError: Reading or writing the file failed.
        yaml.YAMLError: YAML parsing or serialization failed.
    """
    try:
        data = _read_yaml(file_path) or {}
        if content.strip():
            data[title] = content
        else:
            data.pop(title, None)
        _write_yaml(file_path, data)
        return data
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Error updating to yaml file: {exc}")
        raise
