"""JSON encoding at persistence boundaries and decoding of stored row values.

Message content is decoded only when read from SQLite: a plain string such as
``"null"`` or ``"[]"`` is also valid user content and must stay a string in cache.
All row decoders modify the disposable row supplied by their caller.
"""

import json
from typing import Any

JSON_MESSAGE_FIELDS = ("tool_calls", "audio", "annotations", "function_call")


def encode_json(value: Any, *, default: Any = None) -> str:
    """Encode a native value; strings are data rather than pre-encoded JSON."""
    return json.dumps(default if value is None else value, ensure_ascii=False)


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


def decode_message_row(row: dict, *, content_json: bool = False) -> dict:
    fields = JSON_MESSAGE_FIELDS
    if content_json:
        fields = ("content", *fields)
    decode_json_fields(row, fields)
    for key in ("metadata", "extensions"):
        if key in row:
            row[key] = decode_json_object(row[key])
    return row
