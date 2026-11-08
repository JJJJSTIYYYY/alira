"""Cache namespace encoding and identifier validation."""

import pytest

from alira.modules.database.services.cache import keys


def test_cache_key_builders():
    assert keys.build_conversation_key(
        {"user_uid": "u-1", "conversation_uid": "c-1"}
    ) == "memo:u-1:c-1"
    assert keys.build_conversation_key(
        {"user_uid": "u-1", "conversation_uid": "c-1"}, prefix="chain"
    ) == "chain:u-1:c-1"
    assert keys.build_resource_key(
        {"cache_group": "skills:u/1", "cache_key": "list:5"}
    ) == "resource:skills%3Au%2F1:list%3A5"
    assert keys.build_resource_prefix(
        {"cache_group": "skills:u/1"}
    ) == "resource:skills%3Au%2F1:"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_uid": "u-1"},
        {"conversation_uid": "c-1"},
        {"user_uid": "", "conversation_uid": "c-1"},
    ],
)
def test_cache_key_builder_requires_nonempty_identifiers(payload):
    with pytest.raises(KeyError, match="user_uid and conversation_uid required"):
        keys.build_conversation_key(payload)


@pytest.mark.parametrize(
    ("method_name", "payload", "message"),
    [
        ("build_resource_prefix", {}, "cache_group"),
        ("build_resource_prefix", {"cache_group": ""}, "cache_group"),
        (
            "build_resource_key",
            {"cache_group": "skills", "cache_key": ""},
            "cache_key",
        ),
    ],
)
def test_resource_key_builder_requires_nonempty_parts(
    method_name,
    payload,
    message,
):
    with pytest.raises(KeyError, match=message):
        getattr(keys, method_name)(payload)
