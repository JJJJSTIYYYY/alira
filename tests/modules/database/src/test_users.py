"""Public local-user registration, lookup, and database response contracts."""

import asyncio
from uuid import UUID

import pytest

from alira.modules.database.src import StoreManager, query_store
from alira.modules.database.src import manager as manager_module
from alira.modules.database.src.executor import StoreExecutor
from alira.modules.database.src.utils.errors import (
    StoreCode,
    business_error_result,
    failure_result,
)


@pytest.fixture
async def store(tmp_path, monkeypatch):
    manager = StoreManager(
        db_path=tmp_path / "users.sqlite3",
        cache_path=tmp_path / "cache.json",
        worker_count=2,
    )
    monkeypatch.setattr(manager_module, "_store_manager", manager)
    try:
        yield manager
    finally:
        await manager.stop()


async def test_registration_login_listing_and_restart(store):
    assert await query_store("list_all_user", {}) == {
        "success": True,
        "code": 0,
        "messages": [],
    }
    payload = {"user_name": "alice", "user_uid": "caller-supplied"}
    created = await query_store("create_user", payload)
    identity = created["messages"]
    assert created == {"success": True, "code": 0, "messages": identity}
    assert set(identity) == {"user_uid", "user_name"}
    assert UUID(identity["user_uid"]).version == 4
    assert identity["user_name"] == "alice"
    assert payload == {"user_name": "alice", "user_uid": "caller-supplied"}
    for lookup in (
        {"user_uid": identity["user_uid"]},
        {"user_name": "alice"},
        identity,
    ):
        assert await query_store("verify_user", lookup) == created
    assert await query_store(
        "verify_user", {"user_uid": "missing", "user_name": "alice"}
    ) == business_error_result(StoreCode.USER_NOT_FOUND)
    second = await query_store("create_user", {"user_name": "bob"})
    assert second["messages"]["user_uid"] != identity["user_uid"]
    assert await query_store(
        "verify_user", {"user_uid": identity["user_uid"], "user_name": "bob"}
    ) == business_error_result(StoreCode.USER_NOT_FOUND)
    await store.stop()
    assert await query_store("verify_user", {"user_name": "alice"}) == created
    assert await query_store("list_all_user", {}) == {
        "success": True,
        "code": 0,
        "messages": [identity, second["messages"]],
    }
    assert await query_store("ensure_user_exists", identity) == failure_result(
        StoreCode.UNKNOWN_ACTION
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"user_name": ""}, {"user_name": "  "}, {"user_name": None}, {"user_name": 1}],
)
async def test_registration_rejects_invalid_names(store, payload):
    assert await query_store("create_user", payload) == business_error_result(
        StoreCode.INVALID_PAYLOAD
    )
    assert (await query_store("list_all_user", {}))["messages"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_uid": ""},
        {"user_name": " "},
        {"user_uid": None},
        {"user_name": 1},
        {"user_name": "alice", "user_uid": None},
    ],
)
async def test_verification_rejects_invalid_identifiers(store, payload):
    assert await query_store("verify_user", payload) == business_error_result(
        StoreCode.INVALID_PAYLOAD
    )


@pytest.mark.parametrize("payload", [{"user_uid": "missing"}, {"user_name": "missing"}])
async def test_verification_fails_for_unknown_users(store, payload):
    assert await query_store("verify_user", payload) == business_error_result(
        StoreCode.USER_NOT_FOUND
    )


async def test_concurrent_duplicate_registration_has_one_winner(store):
    results = await asyncio.gather(
        *(query_store("create_user", {"user_name": "alice"}) for _ in range(8))
    )
    assert sum(result["code"] == 0 for result in results) == 1
    assert all(
        result == business_error_result(StoreCode.USER_ALREADY_EXISTS)
        for result in results
        if result["code"] != 0
    )
    assert len((await query_store("list_all_user", {}))["messages"]) == 1


async def test_every_action_uses_the_public_envelope(store, monkeypatch):
    actions = []
    original = StoreExecutor.export_handlers

    def export(executor):
        handlers = original(executor)
        actions.extend(handlers)
        return handlers

    monkeypatch.setattr(StoreExecutor, "export_handlers", export)
    await store.start()
    assert "ensure_user_exists" not in actions
    for action in actions:
        result = await query_store(action, {})
        assert set(result) == {"success", "code", "messages"}, (action, result)
        assert type(result["success"]) is bool
        assert type(result["code"]) is int
        if result["code"] == 0:
            assert result["success"] is True
            assert isinstance(result["messages"], (dict, list)), (action, result)
        elif result["success"]:
            assert result == business_error_result(StoreCode(result["code"])), (
                action,
                result,
            )
        else:
            assert result == failure_result(StoreCode(result["code"])), (action, result)
