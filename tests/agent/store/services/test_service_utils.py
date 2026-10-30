from unittest.mock import Mock
from uuid import UUID

import pytest

from apix.agent.store.core.services.cache import CacheService
from apix.agent.store.core.services.data import DataService
from apix.agent.store.core import handlers as service_utils
from apix.agent.store.core.handlers import service_handler


def test_cache_key_builders(tmp_path):
    service = CacheService(tmp_path / "cache.json")

    assert service._build_conversation_key(
        {"user_uid": "u-1", "conversation_uid": "c-1"}
    ) == "memo:u-1:c-1"
    assert service._build_conversation_key(
        {"user_uid": "u-1", "conversation_uid": "c-1"}, prefix="chain"
    ) == "chain:u-1:c-1"
    assert service._build_resource_key(
        {"cache_group": "skills:u/1", "cache_key": "list:5"}
    ) == "resource:skills%3Au%2F1:list%3A5"
    assert service._build_resource_prefix(
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
def test_cache_key_builder_requires_nonempty_identifiers(tmp_path, payload):
    with pytest.raises(KeyError, match="user_uid and conversation_uid required"):
        CacheService(tmp_path / "cache.json")._build_conversation_key(payload)


@pytest.mark.parametrize(
    ("method_name", "payload", "message"),
    [
        ("_build_resource_prefix", {}, "cache_group"),
        ("_build_resource_prefix", {"cache_group": ""}, "cache_group"),
        (
            "_build_resource_key",
            {"cache_group": "skills", "cache_key": ""},
            "cache_key",
        ),
    ],
)
def test_resource_key_builder_requires_nonempty_parts(
    tmp_path,
    method_name,
    payload,
    message,
):
    with pytest.raises(KeyError, match=message):
        getattr(CacheService(tmp_path / "cache.json"), method_name)(payload)


def test_data_service_generates_uuid4_strings(tmp_path):
    service = DataService(tmp_path / "data.sqlite3")

    identifiers = [service._new_conversation_id() for _ in range(100)]

    assert len(set(identifiers)) == len(identifiers)
    for identifier in identifiers:
        assert isinstance(identifier, str)
        parsed = UUID(identifier)
        assert parsed.version == 4
        assert str(parsed) == identifier


@pytest.mark.asyncio
async def test_service_handler_logs_and_normalizes_failures(monkeypatch):
    exception = Mock()
    monkeypatch.setattr(service_utils.logger, "exception", exception)

    @service_handler
    async def successful(value):
        return {"success": True, "messages": value}

    @service_handler
    async def failing():
        raise ValueError("service unavailable")

    @service_handler(
        failure_factory=lambda exc: {
            "success": False,
            "messages": {"error": type(exc).__name__},
        }
    )
    async def custom_failure():
        raise KeyError("missing identity")

    assert successful.__name__ == "successful"
    assert await successful("stored") == {
        "success": True,
        "messages": "stored",
    }
    assert await failing() == {
        "success": False,
        "messages": "fail: service unavailable",
    }
    assert await custom_failure() == {
        "success": False,
        "messages": {"error": "KeyError"},
    }
    assert exception.call_count == 2
