"""Dictionary merges preserve values and give the result independent ownership."""

from copy import deepcopy

import pytest

from alira.utils.merge import merge_dicts


def test_nested_mappings_merge_and_lists_are_replaced():
    left = {"kept": 1, "nested": {"old": 2, "changed": 3}, "items": [1, 2]}
    right = {"added": 4, "nested": {"changed": 5, "new": 6}, "items": [3]}

    assert merge_dicts(left, right) == {
        "kept": 1,
        "added": 4,
        "nested": {"old": 2, "changed": 5, "new": 6},
        "items": [3],
    }


@pytest.mark.parametrize(
    "left,right",
    [
        ({"old": 1}, None),
        ({"old": 1}, []),
        (1, {"new": 2}),
        ("old", ""),
        (True, False),
        (1, 0),
    ],
)
def test_right_values_replace_other_types_and_preserve_falsy_values(left, right):
    assert merge_dicts({"value": left}, {"value": right}) == {"value": right}


@pytest.mark.parametrize("left,right", [({}, {}), ({"a": 1}, {}), ({}, {"b": 2})])
def test_empty_inputs(left, right):
    result = merge_dicts(left, right)
    assert result == {**left, **right}
    assert result is not left and result is not right


def test_nested_mutable_values_are_independent_of_both_inputs():
    left = {"retained": {"items": [1]}, "nested": {"left": [2]}}
    right = {"added": {"items": [3]}, "nested": {"right": [4]}}
    before_left, before_right = deepcopy(left), deepcopy(right)

    result = merge_dicts(left, right)
    assert left == before_left and right == before_right
    result["retained"]["items"].append(10)
    result["added"]["items"].append(20)
    result["nested"]["left"].append(30)
    result["nested"]["right"].append(40)
    assert left == before_left and right == before_right

    left["retained"]["items"].append(50)
    right["added"]["items"].append(60)
    assert result["retained"]["items"] == [1, 10]
    assert result["added"]["items"] == [3, 20]
