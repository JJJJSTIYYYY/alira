"""Structural validation for TypedDict data at application boundaries."""

from collections.abc import Mapping, Sized
from typing import is_typeddict

_MISSING = object()


def validate_typed_dict_fields(
    data: Mapping[str, object],
    schema: type,
    *,
    non_empty: bool = False,
) -> None:
    """Check required fields declared by a TypedDict, including inherited keys.

    Optional and extra fields are allowed. Values are neither type-checked nor
    converted. With ``non_empty=True``, required values must not be None or
    empty strings/containers; zero and False remain valid.

    Raises:
        TypeError: If data is not a mapping or schema is not a TypedDict.
        ValueError: If required fields are missing or disallowed empty values.
    """
    if not is_typeddict(schema):
        raise TypeError("schema must be a TypedDict class")
    if not isinstance(data, Mapping):
        raise TypeError("data must be a mapping")

    invalid_fields = []
    for name in schema.__required_keys__:
        value = data.get(name, _MISSING)
        if value is _MISSING or (
            non_empty
            and (value is None or isinstance(value, Sized) and len(value) == 0)
        ):
            invalid_fields.append(name)

    if invalid_fields:
        requirement = "missing or empty" if non_empty else "missing"
        raise ValueError(
            f"{schema.__name__} required fields {requirement}: "
            f"{', '.join(sorted(invalid_fields))}"
        )
