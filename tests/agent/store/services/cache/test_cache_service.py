import json

import pytest

from alira.database.services.cache import CacheService
from alira.database.services.cache import service as cache_module


@pytest.fixture
def payload():
    return {"user_uid": "user-1", "conversation_uid": "conversation-1"}


@pytest.mark.asyncio
async def test_message_cache_workflow_is_copy_safe(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")
    original = [
        {
            "message_uid": "message-1",
            "role": "user",
            "content": "hello",
            "metadata": {},
            "extensions": {},
        }
    ]

    assert await service.set_messages({**payload, "messages": original}) == {
        "success": True,
        "messages": "success",
    }
    original[0]["content"] = "mutated outside cache"

    result = await service.get_messages(payload)
    assert result == {
        "success": True,
        "messages": [
            {
                "message_uid": "message-1",
                "role": "user",
                "content": "hello",
                "metadata": {},
                "extensions": {},
            }
        ],
        "cache_hit": True,
    }

    result["messages"][0]["content"] = "mutated result"
    await service.append_message(
        {
            **payload,
            "message": {
                "message_uid": "message-2",
                "role": "assistant",
                "content": "world",
                "metadata": {"model": "test"},
                "extensions": {"reasoning": "because"},
            },
        }
    )
    result = await service.get_messages(payload)
    assert [message["content"] for message in result["messages"]] == [
        "hello",
        "world",
    ]

    assert await service.delete_messages(payload) == {
        "success": True,
        "messages": "success",
    }
    assert await service.get_messages(payload) == {
        "success": True,
        "messages": [],
        "cache_hit": False,
    }


@pytest.mark.asyncio
async def test_append_is_a_noop_on_cache_miss_and_empty_backfill_clears(
    tmp_path, payload
):
    service = CacheService(tmp_path / "cache.json")

    assert (
        await service.append_message(
            {**payload, "message": {"message_uid": "message-1"}}
        )
    )["success"]
    assert not (await service.get_messages(payload))["cache_hit"]

    await service.set_messages({**payload, "messages": [{"message_uid": "message-1"}]})
    await service.set_messages({**payload, "messages": []})
    assert not (await service.get_messages(payload))["cache_hit"]


@pytest.mark.asyncio
async def test_borrowed_snapshot_has_stable_membership_during_cache_changes(
    tmp_path, payload
):
    service = CacheService(tmp_path / "cache.json")
    first = {"message_uid": "m1", "metadata": {"nested": [1]}}
    await service.set_messages({**payload, "messages": [first]})
    snapshot = await service.get_messages(payload, borrow_records=True)
    await service.append_message({**payload, "message": {"message_uid": "m2"}})
    assert [row["message_uid"] for row in snapshot["messages"]] == ["m1"]

    snapshot["messages"].clear()
    ordinary = await service.get_messages(payload)
    ordinary["messages"][0]["metadata"]["nested"].append(2)
    assert len((await service.get_messages(payload))["messages"]) == 2
    assert (await service.get_messages(payload))["messages"][0]["metadata"] == {
        "nested": [1]
    }


@pytest.mark.asyncio
async def test_restored_message_records_are_normalized_before_reads(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "memo:user-1:conversation-1": {
                    "value": [
                        {
                            "content": "null",
                            "metadata": None,
                            "extensions": '{"task":{"type":"review"}}',
                            "item": '{"id":"item-1"}',
                        }
                    ],
                    "expires_at": 10**20,
                }
            }
        ),
        encoding="utf-8",
    )
    service = CacheService(path)
    await service.start()
    result = await service.get_messages(payload, borrow_records=True)
    assert result["messages"] == [
        {
            "content": "null",
            "metadata": {},
            "extensions": {"task": {"type": "review"}},
            "item": {"id": "item-1"},
        }
    ]


@pytest.mark.asyncio
async def test_ttl_expiry_is_applied_without_sleep(monkeypatch, tmp_path, payload):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    service = CacheService(tmp_path / "cache.json")

    await service.set_messages({**payload, "messages": [{"message_uid": "message-1"}]})
    await service.set_messages_ttl(payload, ttl_seconds=10)
    clock[0] = 1009.9
    assert (await service.get_messages(payload))["cache_hit"]
    clock[0] = 1010.0
    assert not (await service.get_messages(payload))["cache_hit"]
    assert service._cache == {}


@pytest.mark.asyncio
async def test_branch_chain_create_update_and_read(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")

    assert await service.set_branch_chain(
        {**payload, "node_id_chain": ["root", "old"]}
    ) == {"success": True, "messages": "success"}
    assert await service.get_branch_chain(payload) == {
        "success": True,
        "messages": ["root", "old"],
        "cache_hit": True,
    }
    assert await service.update_branch_chain(
        {**payload, "parent_id": "root", "node_id": "new"}
    ) == {"success": True, "messages": "success"}
    assert (await service.get_branch_chain(payload))["messages"] == [
        "root",
        "new",
    ]
    assert await service.update_branch_chain(
        {**payload, "parent_id": "root", "node_id": "new"}
    ) == {"success": True, "messages": "already up to date"}
    assert await service.update_branch_chain(
        {**payload, "parent_id": "missing", "node_id": "other"}
    ) == {"success": True, "messages": "parent_id not found in cache"}


@pytest.mark.asyncio
async def test_branch_chain_update_handles_cache_miss(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")

    assert await service.update_branch_chain(
        {**payload, "parent_id": "root", "node_id": "new"}
    ) == {"success": True, "messages": "cache not found"}
    assert await service.get_branch_chain(payload) == {
        "success": True,
        "messages": [],
        "cache_hit": False,
    }


@pytest.mark.asyncio
async def test_resource_cache_preserves_empty_hits_and_supports_invalidation(
    tmp_path,
):
    service = CacheService(tmp_path / "cache.json")
    first = {
        "cache_group": "skills:user-1",
        "cache_key": "list:5",
        "messages": [],
    }
    second = {
        "cache_group": "skills:user-1",
        "cache_key": "item:skill-1",
        "messages": [{"skill_id": "skill-1"}],
    }
    other_user = {
        "cache_group": "skills:user-2",
        "cache_key": "list:5",
        "messages": [{"skill_id": "skill-2"}],
    }

    for item in (first, second, other_user):
        assert (await service.set_resource(item))["success"]

    empty_hit = await service.get_resource(first)
    assert empty_hit == {
        "success": True,
        "messages": [],
        "cache_hit": True,
    }

    item_hit = await service.get_resource(second)
    item_hit["messages"][0]["skill_id"] = "mutated"
    assert (await service.get_resource(second))["messages"] == [{"skill_id": "skill-1"}]

    await service.delete_resources(
        {
            "cache_group": "skills:user-1",
            "cache_key": "item:skill-1",
        }
    )
    assert not (await service.get_resource(second))["cache_hit"]
    assert (await service.get_resource(first))["cache_hit"]

    await service.delete_resources({"cache_group": "skills:user-1"})
    assert not (await service.get_resource(first))["cache_hit"]
    assert (await service.get_resource(other_user))["cache_hit"]


@pytest.mark.asyncio
async def test_resource_cache_validation_errors_are_failures(tmp_path):
    service = CacheService(tmp_path / "cache.json")

    assert not (
        await service.set_resource(
            {
                "cache_group": "providers:list",
                "cache_key": "user-1",
                "messages": {},
            }
        )
    )["success"]
    assert not (await service.get_resource({}))["success"]
    assert not (await service.delete_resources({}))["success"]


@pytest.mark.asyncio
async def test_persistence_restores_only_valid_unexpired_items(
    monkeypatch, tmp_path, payload
):
    clock = [2000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    path = tmp_path / "nested" / "cache.json"
    first = CacheService(path)
    await first.set_messages({**payload, "messages": [{"message_uid": "message-1"}]})
    await first.set_branch_chain({**payload, "node_id_chain": ["root"]})
    first._cache["resource:expired:key"] = {"value": ["old"], "expires_at": 1999.0}
    await first.stop()

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "resource:expired:key" not in stored
    restored = CacheService(path)
    await restored.start()
    assert (await restored.get_messages(payload))["messages"] == [
        {"message_uid": "message-1"}
    ]
    assert (await restored.get_branch_chain(payload))["messages"] == ["root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["not json", "[]", '{"bad": {}}'])
async def test_invalid_persistence_is_ignored(tmp_path, content):
    path = tmp_path / "cache.json"
    path.write_text(content, encoding="utf-8")
    service = CacheService(path)

    await service.start()

    assert service._cache == {}


@pytest.mark.asyncio
async def test_start_is_idempotent_and_preserves_live_values(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "memo:user-1:conversation-1": {
                    "value": [{"content": "saved"}],
                    "expires_at": 10**20,
                },
                "resource:saved:key": {"value": [], "expires_at": 10**20},
            }
        ),
        encoding="utf-8",
    )
    service = CacheService(path)
    await service.set_messages({**payload, "messages": [{"content": "live"}]})

    await service.start()
    assert (await service.get_messages(payload))["messages"] == [{"content": "live"}]
    assert (await service.get_resource({"cache_group": "saved", "cache_key": "key"}))[
        "cache_hit"
    ]

    await service.delete_resources({"cache_group": "saved"})
    await service.start()
    assert not (
        await service.get_resource({"cache_group": "saved", "cache_key": "key"})
    )["cache_hit"]
    assert (await service.get_messages(payload))["messages"] == [{"content": "live"}]


@pytest.mark.asyncio
async def test_corrupt_persistence_does_not_clear_live_values(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text("not JSON", encoding="utf-8")
    service = CacheService(path)
    await service.set_messages({**payload, "messages": [{"content": "live"}]})

    await service.start()

    assert (await service.get_messages(payload))["messages"] == [{"content": "live"}]


@pytest.mark.asyncio
async def test_persistence_rejects_invalid_shapes_but_keeps_generic_resource_lists(
    tmp_path,
):
    path = tmp_path / "cache.json"
    future = 10**20
    path.write_text(
        json.dumps(
            {
                "memo:u:bad-type": {"value": {}, "expires_at": future},
                "memo:u:bad-item": {"value": ["not an object"], "expires_at": future},
                "chain:u:bad-item": {"value": [{}], "expires_at": future},
                "resource:g:bad-type": {"value": {}, "expires_at": future},
                "resource:g:bad-ttl": {"value": [], "expires_at": "later"},
                "resource:g:infinite-ttl": {"value": [], "expires_at": float("inf")},
                "unknown:key": {"value": [], "expires_at": future},
                "resource:g:valid": {
                    "value": [1, None, {"a": []}],
                    "expires_at": future,
                },
            }
        ),
        encoding="utf-8",
    )
    service = CacheService(path)

    await service.start()

    assert set(service._cache) == {"resource:g:valid"}
    assert await service.get_resource({"cache_group": "g", "cache_key": "valid"}) == {
        "success": True,
        "messages": [1, None, {"a": []}],
        "cache_hit": True,
    }


@pytest.mark.asyncio
async def test_same_instance_can_restore_after_stop(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")
    await service.start()
    await service.set_messages({**payload, "messages": [{"content": "saved"}]})
    await service.stop()
    await service.delete_messages(payload)

    await service.start()

    assert (await service.get_messages(payload))["messages"] == [{"content": "saved"}]


@pytest.mark.asyncio
async def test_empty_branch_chain_remains_a_copy_safe_hit(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")
    chain = ["root"]
    await service.set_branch_chain({**payload, "node_id_chain": chain})
    chain.append("external")
    result = await service.get_branch_chain(payload)
    result["messages"].append("result")
    assert (await service.get_branch_chain(payload))["messages"] == ["root"]

    await service.set_branch_chain({**payload, "node_id_chain": []})
    assert await service.get_branch_chain(payload) == {
        "success": True,
        "messages": [],
        "cache_hit": True,
    }


@pytest.mark.asyncio
async def test_validation_errors_are_returned_as_failures(tmp_path, payload):
    service = CacheService(tmp_path / "cache.json")

    for result in (
        await service.set_messages({**payload, "messages": "not-a-list"}),
        await service.set_messages({**payload, "messages": ["not-an-object"]}),
        await service.append_message({**payload, "message": []}),
        await service.set_branch_chain({**payload, "node_id_chain": "not-a-list"}),
        await service.set_branch_chain({**payload, "node_id_chain": [{}]}),
        await service.update_branch_chain({**payload, "node_id": [], "parent_id": "-"}),
        await service.get_messages({"user_uid": "user-1"}),
        await service.update_branch_chain(payload),
    ):
        assert result["success"] is False
        assert result["messages"].startswith("fail:")


def test_item_validation_and_purge_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module.time, "time", lambda: 50.0)
    service = CacheService(tmp_path / "cache.json")
    service._cache = {
        "resource:g:valid": {"value": [1], "expires_at": 51.0},
        "resource:g:expired": {"value": [2], "expires_at": 49.0},
        "resource:g:malformed": {"value": [3]},
    }

    service._purge_expired()

    assert service._cache == {"resource:g:valid": {"value": [1], "expires_at": 51.0}}
