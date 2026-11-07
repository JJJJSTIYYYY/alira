"""JSON encoding at persistence boundaries and decoding of stored row values.

Message content is decoded only when read from SQLite: a plain string such as
``"null"`` or ``"[]"`` is also valid user content and must stay a string in cache.
All row decoders modify the disposable row supplied by their caller.
"""

import json
import re
from typing import Any

from alira.utils.serialization import json_default

JSON_MESSAGE_FIELDS = ("item", "origin")


def encode_json(value: Any, *, default: Any = None) -> str:
    """Encode a native value; strings are data rather than pre-encoded JSON."""
    return json.dumps(
        default if value is None else value, ensure_ascii=False, default=json_default
    )


def encode_optional_json(value: Any) -> str | None:
    """Keep SQL NULL distinct from a JSON-encoded value."""
    return None if value is None else encode_json(value)


def decode_json_fields(row: dict, fields: tuple[str, ...]) -> dict:
    """Decode explicitly selected database JSON columns once."""
    for key in fields:
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


def decode_json_object(value: Any) -> dict[str, Any]:
    """Normalize optional message metadata to an object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def decode_message_row(row: dict) -> dict:
    """Normalize optional JSON fields in a newly owned record."""
    decode_json_fields(row, JSON_MESSAGE_FIELDS)
    for key in ("metadata", "extensions"):
        if key in row:
            row[key] = decode_json_object(row[key])
    return row


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize SQLite timestamps to ISO format."""
    for key, value in row.items():
        if not isinstance(value, str):
            continue
        if (key.endswith("_at") or key in {"exec_time", "timestamp"}) and re.match(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value
        ):
            row[key] = value.replace(" ", "T", 1)
    return row
