"""TypedDict required-field rules and non-empty validation are independent."""

from types import MappingProxyType
from typing import NotRequired, Required, TypedDict

import pytest

from alira.utils.validation import validate_typed_dict_fields


class Options(TypedDict, total=False):
    label: str
    count: Required[int]


class Request(Options):
    enabled: bool
    note: NotRequired[str]


def test_required_fields_follow_inheritance_and_qualifiers():
    data = MappingProxyType({"count": 0, "enabled": False, "extra": "allowed"})
    validate_typed_dict_fields(data, Request, non_empty=True)
    with pytest.raises(ValueError, match="count, enabled"):
        validate_typed_dict_fields({"label": "optional", "note": "optional"}, Request)


def test_total_false_allows_empty_data():
    class OptionalFields(TypedDict, total=False):
        value: str

    validate_typed_dict_fields({}, OptionalFields, non_empty=True)


@pytest.mark.parametrize("value", [None, "", [], {}, (), set(), b""])
def test_empty_values_are_rejected_only_when_requested(value):
    validate_typed_dict_fields({"count": value}, Options)
    with pytest.raises(ValueError, match="count"):
        validate_typed_dict_fields({"count": value}, Options, non_empty=True)


@pytest.mark.parametrize("value", [0, False, "value", [0]])
def test_non_empty_check_preserves_present_values(value):
    validate_typed_dict_fields({"count": value}, Options, non_empty=True)


@pytest.mark.parametrize("data", [None, [], "data"])
def test_non_mapping_input_is_rejected(data):
    with pytest.raises(TypeError, match="mapping"):
        validate_typed_dict_fields(data, Options)


def test_schema_must_be_a_typed_dict():
    with pytest.raises(TypeError, match="TypedDict"):
        validate_typed_dict_fields({}, dict)
