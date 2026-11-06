"""JSON support for typed middleware and SDK provenance at storage boundaries."""

from dataclasses import fields, is_dataclass
from typing import Any

from pydantic import BaseModel


def json_default(value: Any) -> Any:
    """Serialize the typed objects retained in middleware origin values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
