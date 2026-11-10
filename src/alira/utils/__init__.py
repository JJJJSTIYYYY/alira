"""Business-agnostic helpers with no dependency on application layers."""

from typing import TYPE_CHECKING

from alira.utils.config import YamlConfig
from alira.utils.merge import merge_dicts
from alira.utils.validation import validate_typed_dict_fields
from alira.utils.yaml import append_to_yaml, load_from_yaml, update_to_yaml, write_to_yaml

if TYPE_CHECKING:
    from alira.utils.serialization import json_default

__all__ = [
    "YamlConfig",
    "append_to_yaml",
    "json_default",
    "load_from_yaml",
    "merge_dicts",
    "update_to_yaml",
    "validate_typed_dict_fields",
    "write_to_yaml",
]


def __getattr__(name: str):
    # Pydantic is a business dependency, unnecessary for kernel-only startup.
    if name == "json_default":
        from alira.utils.serialization import json_default

        return json_default
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
