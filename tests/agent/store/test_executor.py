"""Comprehensive tests for the store execution-layer orchestration."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from alira.agent.store import executor as execute_module
from alira.agent.store.executor import StoreExecutor
from alira.agent.store.services.cache import CacheService


def _executor(
    *,
    data_store=None,
    cache_store=None,
    file_store=None,
) -> StoreExecutor:
    defaults = CacheService()
    if cache_store is None:
        cache_store = defaults
    else:
        for name in (
            "get_resource",
            "set_resource",
            "delete_resources",
            "get_messages",
            "set_messages",
            "delete_messages",
            "append_message",
            "get_branch_chain",
            "set_branch_chain",
            "update_branch_chain",
        ):
            if not hasattr(cache_store, name):
                setattr(cache_store, name, getattr(defaults, name))
    return StoreExecutor(
        data_store=data_store or SimpleNamespace(),
        cache_store=cache_store,
        file_store=file_store or SimpleNamespace(),
    )


@pytest.fixture
def message_nodes(monkeypatch):
    helper = Mock()
    monkeypatch.setattr(execute_module, "MessageNodeTree", Mock(return_value=helper))
    return helper


def _row(
    node_id: str,
    parent_id: str,
    cursor: int,
    role: str,
    *,
    deleted: bool = False,
    metadata=None,
    extensions=None,
) -> dict:
    return {
        "node_id": node_id,
        "parent_id": parent_id,
        "msg_cursor": cursor,
        "role": role,
        "is_deleted": deleted,
        "metadata": {} if metadata is None else metadata,
        "extensions": {} if extensions is None else extensions,
        "content": f"{node_id}:{cursor}",
    }


def test_export_handlers_returns_only_decorated_async_methods():
    executor = _executor()
    executor.public_value = 1
    executor.public_callable = lambda: None

    handlers = executor.export_handlers()

    assert handlers["append_message"] == executor.append_message
    assert handlers["list_messages"] == executor.list_messages
    assert handlers["create_cron_task"] == executor.create_cron_task
    assert "export_handlers" not in handlers
    assert "public_callable" not in handlers
    assert "public_value" not in handlers


def test_export_handlers_rejects_decorated_sync_method(monkeypatch):
    def invalid_handler(self, payload):
        return payload

    invalid_handler._handler_name = "invalid_handler"
    monkeypatch.setattr(
        StoreExecutor,
        "invalid_handler",
        invalid_handler,
        raising=False,
    )

    with pytest.raises(TypeError, match="must be async"):
        _executor().export_handlers()


@pytest.mark.asyncio
async def test_message_projection_decodes_rows_without_changing_cached_values():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "current_node_id": "node-a",
    }
    rows = [
        _row(
            "node-a",
            "-",
            1,
            "user",
            metadata="invalid-json",
            extensions='{"source":"json"}',
        ),
        _row("node-b", "-", 2, "user"),
        _row(
            "node-c",
            "node-a",
            3,
            "assistant",
            metadata='{"score":1}',
            extensions="invalid-json",
        ),
    ]
    rows[2]["content"] = "null"
    rows[2]["tool_calls"] = '[{"id":"call-1"}]'
    cache = CacheService()
    await cache.set_messages({**payload, "messages": rows})

    result = await _executor(cache_store=cache).list_user_messages(payload)

    assert result["success"]
    assert [row["node_id"] for row in result["messages"]] == ["node-a", "node-c"]
    assert result["messages"][0]["metadata"] == {}
    assert result["messages"][0]["extensions"] == {"source": "json"}
    assert result["messages"][1]["metadata"] == {"score": 1}
    assert result["messages"][1]["extensions"] == {}
    assert result["messages"][1]["content"] == "null"
    assert result["messages"][1]["tool_calls"] == [{"id": "call-1"}]
    assert result["current_chain"] == ["node-a", "node-c"]
    assert result["branches"]["-"] == [
        {"node_id": "node-a", "cursor": 1},
        {"node_id": "node-b", "cursor": 2},
    ]
    assert (await cache.get_messages(payload))["messages"] == rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_method", "service_name", "store_method"),
    [
        ("verify_user", "data_store", "verify_user"),
        ("ensure_user_exists", "data_store", "ensure_user_exists"),
        (
            "list_conversations",
            "data_store",
            "list_conversations",
        ),
        (
            "get_conversation",
            "data_store",
            "get_conversation",
        ),
        (
            "search_messages",
            "data_store",
            "search_messages",
        ),
        (
            "get_branch_chain",
            "cache_store",
            "get_branch_chain",
        ),
        (
            "list_shortterm_memories",
            "data_store",
            "list_shortterm_memories",
        ),
        (
            "create_shortterm_memory",
            "data_store",
            "create_shortterm_memory",
        ),
        (
            "list_longterm_memories",
            "data_store",
            "list_longterm_memories",
        ),
        (
            "update_longterm_memory",
            "data_store",
            "update_longterm_memory",
        ),
        ("update_llm_provider", "data_store", "update_llm_provider"),
        ("update_mcp_server", "data_store", "update_mcp_server"),
        ("update_cron_task", "data_store", "update_cron_task"),
    ],
)
async def test_simple_handlers_forward_payload(
    executor_method,
    service_name,
    store_method,
):
    payload = {"value": 1}
    messages = (
        []
        if executor_method
        in {
            "list_shortterm_memories",
            "list_longterm_memories",
        }
        else "ok"
    )
    expected = {"success": True, "messages": messages}
    handler = AsyncMock(return_value=expected)
    service = SimpleNamespace(**{store_method: handler})
    executor = _executor(**{service_name: service})

    assert await getattr(executor, executor_method)(payload) == expected
    handler.assert_awaited_once_with(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_method", "service_name", "store_method"),
    [
        ("verify_user", "data_store", "verify_user"),
        ("ensure_user_exists", "data_store", "ensure_user_exists"),
        (
            "list_conversations",
            "data_store",
            "list_conversations",
        ),
        (
            "get_conversation",
            "data_store",
            "get_conversation",
        ),
        (
            "search_messages",
            "data_store",
            "search_messages",
        ),
        (
            "get_branch_chain",
            "cache_store",
            "get_branch_chain",
        ),
        (
            "list_shortterm_memories",
            "data_store",
            "list_shortterm_memories",
        ),
        (
            "create_shortterm_memory",
            "data_store",
            "create_shortterm_memory",
        ),
        (
            "list_longterm_memories",
            "data_store",
            "list_longterm_memories",
        ),
        (
            "update_longterm_memory",
            "data_store",
            "update_longterm_memory",
        ),
        ("update_llm_provider", "data_store", "update_llm_provider"),
        ("update_mcp_server", "data_store", "update_mcp_server"),
        ("update_cron_task", "data_store", "update_cron_task"),
    ],
)
async def test_simple_handlers_normalize_exceptions(
    executor_method,
    service_name,
    store_method,
):
    handler = AsyncMock(side_effect=RuntimeError("service down"))
    service = SimpleNamespace(**{store_method: handler})
    executor = _executor(**{service_name: service})

    assert await getattr(executor, executor_method)({}) == {
        "success": False,
        "messages": "fail: service down",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_method", "create_method", "ensure_kwargs"),
    [
        ("create_user", "create_user", {"exist": False}),
        (
            "create_conversation",
            "create_conversation",
            {},
        ),
    ],
)
async def test_create_handlers_check_user_then_create(
    executor_method,
    create_method,
    ensure_kwargs,
):
    payload = {"user_uid": "user-1"}
    created = {"success": True, "messages": "created"}
    ensure = AsyncMock(return_value={"success": True})
    create = AsyncMock(return_value=created)
    data_store = SimpleNamespace(
        ensure_user_exists=ensure,
        **{create_method: create},
    )
    executor = _executor(data_store=data_store)

    assert await getattr(executor, executor_method)(payload) == created
    ensure.assert_awaited_once_with(payload, **ensure_kwargs)
    create.assert_awaited_once_with(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_method", "create_method"),
    [
        ("create_user", "create_user"),
        ("create_conversation", "create_conversation"),
    ],
)
async def test_create_handlers_short_circuit_and_normalize_exception(
    executor_method,
    create_method,
):
    failure = {"success": False, "messages": "missing user"}
    ensure = AsyncMock(return_value=failure)
    create = AsyncMock()
    data_store = SimpleNamespace(
        ensure_user_exists=ensure,
        **{create_method: create},
    )
    executor = _executor(data_store=data_store)

    assert await getattr(executor, executor_method)({}) == failure
    create.assert_not_awaited()

    ensure.side_effect = RuntimeError("lookup failed")
    assert await getattr(executor, executor_method)({}) == {
        "success": False,
        "messages": "fail: lookup failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("write_succeeds", [True, False])
async def test_conversation_deletion_invalidates_cache_only_after_persistence(
    write_succeeds,
):
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "is_deleted": True,
    }
    original = deepcopy(payload)
    order = []
    expected = {
        "success": write_succeeds,
        "messages": "saved" if write_succeeds else "write failed",
    }

    async def persist(request):
        order.append("data")
        assert request == original
        return expected

    async def invalidate(request):
        order.append("cache")
        assert request == original
        return {"success": False, "messages": "cache unavailable"}

    cache_store = SimpleNamespace(delete_messages=AsyncMock(side_effect=invalidate))
    executor = _executor(
        data_store=SimpleNamespace(update_conversation=AsyncMock(side_effect=persist)),
        cache_store=cache_store,
    )

    assert await executor.update_conversation(payload) == expected
    assert order == (["data", "cache"] if write_succeeds else ["data"])
    assert payload == original


@pytest.mark.asyncio
async def test_update_conversation_skips_cache_and_normalizes_failure():
    payload = {"is_deleted": False}
    cache_store = SimpleNamespace(
        delete_messages=AsyncMock(return_value={"success": True, "messages": "success"})
    )
    data_store = SimpleNamespace(
        update_conversation=AsyncMock(
            return_value={"success": True, "messages": "success"}
        )
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert (await executor.update_conversation(payload))["success"] is True
    cache_store.delete_messages.assert_not_awaited()

    data_store.update_conversation.side_effect = RuntimeError("update failed")
    assert await executor.update_conversation(payload) == {
        "success": False,
        "messages": "fail: update failed",
    }


@pytest.mark.asyncio
async def test_append_message_persists_and_caches_a_private_snapshot():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "message": {
            "message_uid": "message-1",
            "role": "user",
            "node_id": "node-1",
            "parent_id": "-",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    original = deepcopy(payload)
    persisted = []

    async def persist(write_payload):
        persisted.append(deepcopy(write_payload))
        return {
            "success": True,
            "messages": {"msg_cursor": 3, "timestamp": "2026-01-01T00:00:00"},
        }

    data_store = SimpleNamespace(append_message=AsyncMock(side_effect=persist))
    cache_store = SimpleNamespace(
        append_message=AsyncMock(return_value={"success": True}),
        update_branch_chain=AsyncMock(return_value={"success": True}),
    )
    executor = _executor(data_store=data_store, cache_store=cache_store)

    assert await executor.append_message(payload) == {
        "success": True,
        "messages": "success",
    }
    assert payload == original
    assert persisted == [original]
    write_payload = data_store.append_message.await_args.args[0]
    cached_payload = cache_store.append_message.await_args.args[0]
    assert cached_payload is write_payload
    assert cached_payload is not payload
    assert cached_payload["message"] is not payload["message"]
    assert cached_payload["message"] == {
        **original["message"],
        "msg_cursor": 3,
        "timestamp": "2026-01-01T00:00:00",
        "is_deleted": False,
    }
    cache_store.update_branch_chain.assert_awaited_once_with(
        {
            "user_uid": "user-1",
            "conversation_uid": "conversation-1",
            "node_id": "node-1",
            "parent_id": "-",
        }
    )


@pytest.mark.asyncio
async def test_append_message_keeps_database_and_cache_snapshot_consistent_across_await():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "message": {"content": [{"text": "original"}]},
    }
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist(write_payload):
        assert write_payload["message"]["content"] == [{"text": "original"}]
        entered.set()
        await release.wait()
        assert write_payload["message"]["content"] == [{"text": "original"}]
        return {"success": True, "messages": {"msg_cursor": 1}}

    cache_store = SimpleNamespace(
        append_message=AsyncMock(return_value={"success": True})
    )
    executor = _executor(
        data_store=SimpleNamespace(append_message=AsyncMock(side_effect=persist)),
        cache_store=cache_store,
    )
    task = asyncio.create_task(executor.append_message(payload))
    await entered.wait()
    payload["message"]["content"][0]["text"] = "changed"
    release.set()
    assert (await task)["success"] is True
    assert cache_store.append_message.await_args.args[0]["message"]["content"] == [
        {"text": "original"}
    ]


@pytest.mark.asyncio
async def test_append_message_short_circuits_and_ignores_cache_append_error():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "message": {},
    }
    failure = {"success": False, "messages": "write failed"}
    data_store = SimpleNamespace(append_message=AsyncMock(return_value=failure))
    cache_store = SimpleNamespace(
        append_message=AsyncMock(return_value={"success": True, "messages": "success"}),
        update_branch_chain=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.append_message(deepcopy(payload)) == failure
    cache_store.append_message.assert_not_awaited()

    data_store.append_message.return_value = {
        "success": True,
        "messages": {"msg_cursor": 1},
    }
    cache_store.append_message.return_value = {
        "success": False,
        "messages": "cache down",
    }

    assert (await executor.append_message(payload))["success"] is True
    cache_store.update_branch_chain.assert_awaited_once()


@pytest.mark.asyncio
async def test_append_message_normalizes_outer_exception():
    executor = _executor(
        data_store=SimpleNamespace(
            append_message=AsyncMock(
                return_value={"success": True, "messages": "success"}
            )
        ),
        cache_store=SimpleNamespace(),
    )

    assert await executor.append_message({}) == {
        "success": False,
        "messages": "fail: 'message'",
    }


@pytest.mark.asyncio
async def test_delete_messages_deletes_related_memory_ids():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
    }
    data_store = SimpleNamespace(
        delete_messages=AsyncMock(
            return_value={
                "success": True,
                "messages": [
                    {"message_uid": ""},
                    {"other": "missing"},
                    {"message_uid": "memory-1"},
                ],
            }
        ),
        delete_shortterm_memories=AsyncMock(return_value={"success": True}),
    )
    cache_store = SimpleNamespace(
        delete_messages=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
        delete_resources=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.delete_messages(payload) == {
        "success": True,
        "messages": "success",
    }
    cache_store.delete_messages.assert_awaited_once_with(payload)
    data_store.delete_shortterm_memories.assert_awaited_once_with(
        {
            "user_uid": "user-1",
            "conversation_uid": "conversation-1",
            "memory_ids": ["memory-1"],
        }
    )
    cache_store.delete_resources.assert_awaited_once_with(
        {
            "cache_group": "shortterm-memory:user-1",
            "cache_key": "conversation-1",
        }
    )


@pytest.mark.asyncio
async def test_delete_messages_handles_cache_store_and_memory_failures():
    payload = {"user_uid": "user-1"}
    delete_messages = AsyncMock(return_value={"success": True, "messages": None})
    delete_memory = AsyncMock()
    cache_store = SimpleNamespace(
        delete_messages=AsyncMock(
            return_value={"success": False, "messages": "cache down"}
        ),
        delete_resources=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    data_store = SimpleNamespace(
        delete_messages=delete_messages,
        delete_shortterm_memories=delete_memory,
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert (await executor.delete_messages(payload))["success"] is True
    delete_memory.assert_not_awaited()

    failure = {"success": False, "messages": "delete failed"}
    delete_messages.return_value = failure
    assert await executor.delete_messages(payload) == failure
    assert cache_store.delete_messages.await_count == 1

    delete_messages.return_value = {
        "success": True,
        "messages": [{"message_uid": "memory-1"}],
    }
    memory_failure = {"success": False, "messages": "memory failed"}
    delete_memory.return_value = memory_failure
    assert await executor.delete_messages(payload) == memory_failure
    cache_store.delete_resources.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_messages_normalizes_outer_exception():
    executor = _executor(
        data_store=SimpleNamespace(
            delete_messages=AsyncMock(side_effect=RuntimeError("db down"))
        ),
        cache_store=SimpleNamespace(
            delete_messages=AsyncMock(
                return_value={"success": True, "messages": "success"}
            )
        ),
    )

    assert await executor.delete_messages({}) == {
        "success": False,
        "messages": "fail: db down",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_roles", "expected_guess"),
    [
        (
            "list_messages",
            ("user", "assistant", "system", "tool", "info"),
            False,
        ),
        ("list_user_messages", ("user", "assistant", "info", "tool"), True),
    ],
)
async def test_list_messages_uses_cached_chain_and_recent_messages(
    method_name,
    expected_roles,
    expected_guess,
    message_nodes,
):
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "current_node_id": "-",
    }
    cache_store = SimpleNamespace(
        get_branch_chain=AsyncMock(
            return_value={
                "success": True,
                "cache_hit": True,
                "messages": ["-", "cached-leaf"],
            }
        ),
        get_messages=AsyncMock(
            return_value={
                "success": True,
                "cache_hit": True,
                "messages": [{"raw": True}],
            }
        ),
        set_branch_chain=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    executor = _executor(cache_store=cache_store)
    message_nodes.build_visible_messages = Mock(
        return_value=(
            [{"parsed": True}],
            {"-": [{"node_id": "branch"}]},
            ["-", "cached-leaf"],
        )
    )

    result = await getattr(executor, method_name)(payload)

    assert result["messages"] == [{"parsed": True}]
    assert result["branches"] == {"-": [{"node_id": "branch"}]}
    assert "node_id_chain" not in payload
    assert result["current_chain"] == ["-", "cached-leaf"]
    execute_module.MessageNodeTree.assert_called_once_with([{"raw": True}])
    message_nodes.build_visible_messages.assert_called_once_with(
        "cached-leaf",
        allow_roles=expected_roles,
        guess_children=expected_guess,
    )
    cache_store.set_branch_chain.assert_awaited_once_with(
        {**payload, "node_id_chain": ["-", "cached-leaf"]}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "chain_response"),
    [
        (
            "list_messages",
            {"success": False, "cache_hit": True, "messages": ["ignored"]},
        ),
        (
            "list_messages",
            {"success": True, "cache_hit": True, "messages": []},
        ),
        (
            "list_user_messages",
            {"success": False, "cache_hit": True, "messages": ["ignored"]},
        ),
        (
            "list_user_messages",
            {"success": True, "cache_hit": True, "messages": []},
        ),
    ],
)
async def test_list_messages_ignores_unusable_cached_chains(
    method_name,
    chain_response,
    message_nodes,
):
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "current_node_id": "-",
    }
    cache_store = SimpleNamespace(
        get_branch_chain=AsyncMock(return_value=chain_response),
        get_messages=AsyncMock(
            return_value={
                "success": True,
                "cache_hit": True,
                "messages": [{"raw": True}],
            }
        ),
        set_branch_chain=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    executor = _executor(cache_store=cache_store)
    message_nodes.build_visible_messages = Mock(
        return_value=([{"parsed": True}], {}, [])
    )

    result = await getattr(executor, method_name)(payload)

    assert result["messages"] == [{"parsed": True}]
    assert message_nodes.build_visible_messages.call_args.args[0] == "-"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["list_messages", "list_user_messages"],
)
async def test_list_messages_falls_back_to_database_and_tolerates_cache_errors(
    method_name,
    message_nodes,
):
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "current_node_id": "-",
    }
    raw_messages = [{"raw": True}]
    cache_store = SimpleNamespace(
        get_branch_chain=AsyncMock(
            return_value={"success": False, "messages": "chain cache down"}
        ),
        get_messages=AsyncMock(return_value={"success": True, "cache_hit": False}),
        set_messages=AsyncMock(
            return_value={"success": False, "messages": "message cache down"}
        ),
        set_branch_chain=AsyncMock(
            return_value={"success": True, "messages": "success"}
        ),
    )
    data_store = SimpleNamespace(
        list_messages=AsyncMock(
            return_value={
                "success": True,
                "messages": raw_messages,
            }
        )
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )
    message_nodes.build_visible_messages = Mock(
        return_value=(
            [{"parsed": True}],
            {},
            ["node-1"],
        )
    )

    result = await getattr(executor, method_name)(payload)

    assert result["messages"] == [{"parsed": True}]
    assert "node_id_chain" not in payload
    assert result["current_chain"] == ["node-1"]
    data_store.list_messages.assert_awaited_once_with(
        {
            "user_uid": "user-1",
            "conversation_uid": "conversation-1",
            "current_node_id": "-",
            "cursor": 1,
        }
    )
    cache_store.set_messages.assert_awaited_once_with(
        {
            "user_uid": "user-1",
            "conversation_uid": "conversation-1",
            "current_node_id": "-",
            "messages": raw_messages,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["list_messages", "list_user_messages"],
)
async def test_list_messages_returns_database_failure_or_empty_result(
    method_name,
):
    payload = {"current_node_id": "known"}
    cache_store = SimpleNamespace(
        get_messages=AsyncMock(return_value={"success": False, "cache_hit": False})
    )
    failure = {"success": False, "messages": "db failed"}
    data_store = SimpleNamespace(list_messages=AsyncMock(return_value=failure))
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await getattr(executor, method_name)(payload.copy()) == failure

    empty = {"success": True, "messages": []}
    data_store.list_messages.return_value = empty
    cache_store.get_messages.return_value = {
        "success": True,
        "cache_hit": False,
    }
    assert await getattr(executor, method_name)(payload.copy()) == {
        **empty,
        "branches": {},
        "current_chain": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["list_messages", "list_user_messages"])
async def test_message_readers_normalize_branch_assembly_exception(
    method_name, message_nodes
):
    executor = _executor(
        cache_store=SimpleNamespace(
            get_messages=AsyncMock(
                return_value={
                    "success": True,
                    "cache_hit": True,
                    "messages": [],
                }
            )
        )
    )
    message_nodes.build_visible_messages = Mock(
        side_effect=ValueError("invalid message branch")
    )

    assert await getattr(executor, method_name)({"current_node_id": "known"}) == {
        "success": False,
        "messages": "fail: invalid message branch",
    }


@pytest.mark.asyncio
async def test_create_skills_returns_database_metadata_failure():
    failure = {"success": False, "messages": "metadata failed"}
    executor = _executor(
        data_store=SimpleNamespace(
            ensure_user_exists=AsyncMock(return_value={"success": True}),
            create_skills=AsyncMock(return_value=failure),
        ),
        file_store=SimpleNamespace(
            import_skill_packages=AsyncMock(
                return_value={
                    "success": True,
                    "messages": [{"skill_id": "skill-1"}],
                }
            )
        ),
    )

    assert await executor.create_skills({"user_uid": "user-1"}) == failure


@pytest.mark.asyncio
async def test_create_skills_invalidates_cache_only_after_metadata_is_persisted(
    monkeypatch,
):
    monkeypatch.setattr(
        execute_module,
        "datetime",
        SimpleNamespace(
            now=lambda: SimpleNamespace(strftime=lambda _: "2026-07-29 12:00:00")
        ),
    )
    call_order = []
    skill_info = {
        "skill_id": "skill-1",
        "skill_name": "Search",
        "skill_description": "Search things",
        "skill_version": "1.0.0",
        "package_path": "/skills/search.zip",
        "package_size": 123,
    }

    async def persist(payload):
        call_order.append(("data_store", payload))
        return {"success": True, "messages": "success"}

    async def invalidate(payload):
        call_order.append(("cache", payload))
        return {"success": True, "messages": "success"}

    executor = _executor(
        data_store=SimpleNamespace(
            ensure_user_exists=AsyncMock(return_value={"success": True}),
            create_skills=AsyncMock(side_effect=persist),
        ),
        file_store=SimpleNamespace(
            import_skill_packages=AsyncMock(
                return_value={
                    "success": True,
                    "messages": [skill_info],
                }
            )
        ),
        cache_store=SimpleNamespace(delete_resources=AsyncMock(side_effect=invalidate)),
    )

    result = await executor.create_skills({"user_uid": "user-1"})

    assert result == {
        "success": True,
        "messages": [
            {
                "skill_id": "skill-1",
                "skill_name": "Search",
                "skill_description": "Search things",
                "skill_version": "1.0.0",
                "package_size": 123,
                "is_active": False,
                "upload_at": "2026-07-29 12:00:00",
            }
        ],
    }
    assert call_order == [
        (
            "data_store",
            {
                "user_uid": "user-1",
                "skills": [skill_info],
            },
        ),
        ("cache", {"cache_group": "skills:user-1"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "id_key"),
    [
        ("create_llm_provider", "create_llm_provider", "provider_id"),
        ("create_mcp_server", "create_mcp_server", "mcp_id"),
        ("create_cron_task", "create_cron_task", "task_id"),
        ("create_longterm_memory", "create_longterm_memory", "memory_id"),
    ],
)
async def test_create_metadata_handlers_generate_ids(
    method_name,
    store_method,
    id_key,
    monkeypatch,
):
    monkeypatch.setattr(
        execute_module,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-id"),
    )
    handler = AsyncMock(return_value={"success": True, "messages": "created"})
    payload = {"name": "demo"}
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert (await getattr(executor, method_name)(payload))["success"] is True
    assert id_key not in payload
    handler.assert_awaited_once_with({**payload, id_key: "fixed-id"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        ("create_llm_provider", "create_llm_provider"),
        ("create_mcp_server", "create_mcp_server"),
        ("create_cron_task", "create_cron_task"),
    ],
)
async def test_create_metadata_handlers_normalize_exceptions(
    method_name,
    store_method,
):
    executor = _executor(
        data_store=SimpleNamespace(
            **{store_method: AsyncMock(side_effect=RuntimeError("create failed"))}
        )
    )

    result = await getattr(executor, method_name)({})

    assert result == {
        "success": False,
        "messages": "fail: create failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "id_key", "expected_targets"),
    [
        (
            "create_llm_provider",
            "create_llm_provider",
            "provider_id",
            (
                {
                    "cache_group": "providers:list",
                    "cache_key": "user-1",
                },
                {
                    "cache_group": "providers:item",
                    "cache_key": "fixed-id",
                },
            ),
        ),
        (
            "create_mcp_server",
            "create_mcp_server",
            "mcp_id",
            ({"cache_group": "mcp:user-1"},),
        ),
        (
            "create_longterm_memory",
            "create_longterm_memory",
            "memory_id",
            (
                {
                    "cache_group": "longterm-memory",
                    "cache_key": "user-1",
                },
            ),
        ),
    ],
)
async def test_create_resource_handlers_invalidate_after_success(
    method_name,
    store_method,
    id_key,
    expected_targets,
    monkeypatch,
):
    monkeypatch.setattr(
        execute_module,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-id"),
    )
    call_order = []

    async def persist(payload):
        call_order.append(("data_store", payload[id_key]))
        return {"success": True, "messages": "created"}

    async def invalidate(payload):
        call_order.append(("cache", payload))
        return {"success": True, "messages": "success"}

    executor = _executor(
        data_store=SimpleNamespace(**{store_method: AsyncMock(side_effect=persist)}),
        cache_store=SimpleNamespace(delete_resources=AsyncMock(side_effect=invalidate)),
    )
    payload = {"user_uid": "user-1"}

    assert (await getattr(executor, method_name)(payload))["success"] is True
    assert call_order == [
        ("data_store", "fixed-id"),
        *(("cache", target) for target in expected_targets),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        ("list_llm_providers", "list_llm_providers"),
        ("get_llm_provider", "get_llm_provider"),
    ],
)
async def test_llm_provider_readers_preserve_native_model_lists(
    method_name,
    store_method,
):
    records = [
        {"provider_id": "one", "model_list": ["model-a"]},
        {"provider_id": "two", "model_list": ["model-b"]},
        {"provider_id": "three", "model_list": None},
    ]
    handler = AsyncMock(return_value={"success": True, "messages": records})
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    result = await getattr(executor, method_name)({})

    assert result["messages"][0]["model_list"] == ["model-a"]
    assert result["messages"][1]["model_list"] == ["model-b"]
    assert result["messages"][2]["model_list"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        ("list_llm_providers", "list_llm_providers"),
        ("get_llm_provider", "get_llm_provider"),
    ],
)
async def test_llm_provider_readers_return_store_failures(
    method_name,
    store_method,
):
    failure = {"success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "success": False,
        "messages": "fail: query crashed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        ("list_mcp_servers", "list_mcp_servers"),
        ("list_enabled_mcp_servers", "list_enabled_mcp_servers"),
    ],
)
async def test_mcp_readers_preserve_native_configs(
    method_name,
    store_method,
):
    records = [
        {"mcp_id": "dict", "config": {"url": "local"}},
        {"mcp_id": "list", "config": ["one"]},
        {"mcp_id": "valid", "config": {"url": "remote"}},
        {"mcp_id": "invalid", "config": {}},
        {"mcp_id": "empty", "config": None},
    ]
    handler = AsyncMock(return_value={"success": True, "messages": records})
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    result = await getattr(executor, method_name)({})

    assert result["messages"][0]["config"] == {"url": "local"}
    assert result["messages"][1]["config"] == ["one"]
    assert result["messages"][2]["config"] == {"url": "remote"}
    assert result["messages"][3]["config"] == {}
    assert result["messages"][4]["config"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        ("list_mcp_servers", "list_mcp_servers"),
        ("list_enabled_mcp_servers", "list_enabled_mcp_servers"),
    ],
)
async def test_mcp_readers_return_store_failure_and_exception(
    method_name,
    store_method,
):
    failure = {"success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "success": False,
        "messages": "fail: query crashed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "payload", "store_method", "expected_cache"),
    [
        (
            "list_skills",
            {"user_uid": "user-1", "limit": 10},
            "list_skills",
            {
                "cache_group": "skills:user-1",
                "cache_key": "list:10",
            },
        ),
        (
            "get_skill",
            {"user_uid": "user-1", "skill_id": "skill-1"},
            "get_skill",
            {
                "cache_group": "skills:user-1",
                "cache_key": "item:skill-1",
            },
        ),
        (
            "list_llm_providers",
            {"user_uid": "user-1"},
            "list_llm_providers",
            {
                "cache_group": "providers:list",
                "cache_key": "user-1",
            },
        ),
        (
            "get_llm_provider",
            {"provider_id": "provider-1"},
            "get_llm_provider",
            {
                "cache_group": "providers:item",
                "cache_key": "provider-1",
            },
        ),
        (
            "list_mcp_servers",
            {"user_uid": "user-1"},
            "list_mcp_servers",
            {
                "cache_group": "mcp:user-1",
                "cache_key": "all",
            },
        ),
        (
            "list_enabled_mcp_servers",
            {"user_uid": "user-1"},
            "list_enabled_mcp_servers",
            {
                "cache_group": "mcp:user-1",
                "cache_key": "enabled",
            },
        ),
        (
            "list_shortterm_memories",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
            },
            "list_shortterm_memories",
            {
                "cache_group": "shortterm-memory:user-1",
                "cache_key": "conversation-1",
            },
        ),
        (
            "list_longterm_memories",
            {"user_uid": "user-1"},
            "list_longterm_memories",
            {
                "cache_group": "longterm-memory",
                "cache_key": "user-1",
            },
        ),
    ],
)
async def test_resource_readers_use_cache_hits_without_querying_data_store(
    method_name,
    payload,
    store_method,
    expected_cache,
):
    cached_messages = []
    cache_store = SimpleNamespace(
        get_resource=AsyncMock(
            return_value={
                "success": True,
                "messages": cached_messages,
                "cache_hit": True,
            }
        ),
        set_resource=AsyncMock(return_value={"success": True, "messages": "success"}),
    )
    data_reader = AsyncMock()
    executor = _executor(
        cache_store=cache_store,
        data_store=SimpleNamespace(**{store_method: data_reader}),
    )

    assert await getattr(executor, method_name)(payload) == {
        "success": True,
        "messages": cached_messages,
    }
    cache_store.get_resource.assert_awaited_once_with(expected_cache)
    data_reader.assert_not_awaited()
    cache_store.set_resource.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "method_name",
        "payload",
        "store_method",
        "database_messages",
        "expected_messages",
        "expected_cache",
    ),
    [
        (
            "list_skills",
            {"user_uid": "user-1"},
            "list_skills",
            [{"skill_id": "skill-1"}],
            [{"skill_id": "skill-1"}],
            {
                "cache_group": "skills:user-1",
                "cache_key": "list:5",
            },
        ),
        (
            "list_llm_providers",
            {"user_uid": "user-1"},
            "list_llm_providers",
            [{"provider_id": "provider-1", "model_list": ["model-a"]}],
            [{"provider_id": "provider-1", "model_list": ["model-a"]}],
            {
                "cache_group": "providers:list",
                "cache_key": "user-1",
            },
        ),
        (
            "list_mcp_servers",
            {"user_uid": "user-1"},
            "list_mcp_servers",
            [{"mcp_id": "mcp-1", "config": {"command": "demo"}}],
            [{"mcp_id": "mcp-1", "config": {"command": "demo"}}],
            {
                "cache_group": "mcp:user-1",
                "cache_key": "all",
            },
        ),
        (
            "list_shortterm_memories",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
            },
            "list_shortterm_memories",
            [
                {
                    "memory_id": "message-1",
                    "content": "Recent context",
                    "created_timestamp": 123,
                }
            ],
            [
                {
                    "memory_id": "message-1",
                    "content": "Recent context",
                    "created_timestamp": 123,
                }
            ],
            {
                "cache_group": "shortterm-memory:user-1",
                "cache_key": "conversation-1",
            },
        ),
        (
            "list_longterm_memories",
            {"user_uid": "user-1"},
            "list_longterm_memories",
            [
                {
                    "memory_id": "memory-1",
                    "title": "Preference",
                    "date": "2025-06-07",
                    "content": "Long-lived context",
                    "source": "conversation",
                }
            ],
            [
                {
                    "memory_id": "memory-1",
                    "title": "Preference",
                    "date": "2025-06-07",
                    "content": "Long-lived context",
                    "source": "conversation",
                }
            ],
            {
                "cache_group": "longterm-memory",
                "cache_key": "user-1",
            },
        ),
    ],
)
async def test_resource_readers_backfill_native_data_store_results(
    method_name,
    payload,
    store_method,
    database_messages,
    expected_messages,
    expected_cache,
):
    cache_store = SimpleNamespace(
        get_resource=AsyncMock(
            return_value={
                "success": True,
                "messages": [],
                "cache_hit": False,
            }
        ),
        set_resource=AsyncMock(return_value={"success": True, "messages": "success"}),
    )
    data_reader = AsyncMock(
        return_value={
            "success": True,
            "messages": deepcopy(database_messages),
        }
    )
    executor = _executor(
        cache_store=cache_store,
        data_store=SimpleNamespace(**{store_method: data_reader}),
    )

    result = await getattr(executor, method_name)(payload)

    assert result == {"success": True, "messages": expected_messages}
    data_reader.assert_awaited_once_with(payload)
    cache_store.set_resource.assert_awaited_once_with(
        {
            **expected_cache,
            "messages": expected_messages,
        }
    )


@pytest.mark.asyncio
async def test_resource_read_falls_back_and_ignores_cache_failures():
    payload = {"user_uid": "user-1"}
    data_store = SimpleNamespace(
        list_skills=AsyncMock(
            return_value={
                "success": True,
                "messages": [{"skill_id": "skill-1"}],
            }
        )
    )
    cache_store = SimpleNamespace(
        get_resource=AsyncMock(
            return_value={"success": False, "messages": "cache read down"}
        ),
        set_resource=AsyncMock(
            return_value={
                "success": False,
                "messages": "cache write down",
            }
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.list_skills(payload) == {
        "success": True,
        "messages": [{"skill_id": "skill-1"}],
    }
    data_store.list_skills.assert_awaited_once_with(payload)

    cache_store.get_resource.side_effect = None
    cache_store.get_resource.return_value = {
        "success": False,
        "messages": "cache unavailable",
        "cache_hit": False,
    }
    cache_store.set_resource.return_value = {
        "success": False,
        "messages": "cache write down",
    }
    assert (await executor.list_skills(payload))["success"] is True
    assert data_store.list_skills.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "payload", "expected_targets"),
    [
        (
            "update_skill",
            "update_skill",
            {"user_uid": "user-1", "skill_id": "skill-1"},
            ({"cache_group": "skills:user-1"},),
        ),
        (
            "update_llm_provider",
            "update_llm_provider",
            {"user_uid": "user-1", "provider_id": "provider-1"},
            (
                {
                    "cache_group": "providers:list",
                    "cache_key": "user-1",
                },
                {
                    "cache_group": "providers:item",
                    "cache_key": "provider-1",
                },
            ),
        ),
        (
            "update_mcp_server",
            "update_mcp_server",
            {"user_uid": "user-1", "mcp_id": "mcp-1"},
            ({"cache_group": "mcp:user-1"},),
        ),
        (
            "create_shortterm_memory",
            "create_shortterm_memory",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
                "memory_id": "message-1",
                "content": "Recent context",
            },
            (
                {
                    "cache_group": "shortterm-memory:user-1",
                    "cache_key": "conversation-1",
                },
            ),
        ),
        (
            "update_longterm_memory",
            "update_longterm_memory",
            {"user_uid": "user-1", "memory_id": "memory-1"},
            (
                {
                    "cache_group": "longterm-memory",
                    "cache_key": "user-1",
                },
            ),
        ),
    ],
)
async def test_resource_mutations_persist_before_best_effort_invalidation(
    method_name,
    store_method,
    payload,
    expected_targets,
):
    call_order = []

    async def persist(_):
        call_order.append("data_store")
        return {"success": True, "messages": "success"}

    async def invalidate(target):
        call_order.append(("cache", target))
        return {"success": False, "messages": "cache down"}

    executor = _executor(
        data_store=SimpleNamespace(**{store_method: AsyncMock(side_effect=persist)}),
        cache_store=SimpleNamespace(delete_resources=AsyncMock(side_effect=invalidate)),
    )

    assert (await getattr(executor, method_name)(payload))["success"] is True
    assert call_order == [
        "data_store",
        *(("cache", target) for target in expected_targets),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "payload"),
    [
        (
            "update_skill",
            "update_skill",
            {"user_uid": "user-1", "skill_id": "skill-1"},
        ),
        (
            "update_llm_provider",
            "update_llm_provider",
            {"user_uid": "user-1", "provider_id": "provider-1"},
        ),
        (
            "update_mcp_server",
            "update_mcp_server",
            {"user_uid": "user-1", "mcp_id": "mcp-1"},
        ),
        (
            "create_shortterm_memory",
            "create_shortterm_memory",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
                "memory_id": "message-1",
                "content": "Recent context",
            },
        ),
        (
            "update_longterm_memory",
            "update_longterm_memory",
            {"user_uid": "user-1", "memory_id": "memory-1"},
        ),
        (
            "create_longterm_memory",
            "create_longterm_memory",
            {"user_uid": "user-1"},
        ),
    ],
)
async def test_failed_resource_mutations_do_not_invalidate_cache(
    method_name,
    store_method,
    payload,
):
    failure = {"success": False, "messages": "database write failed"}
    cache_store = SimpleNamespace(
        delete_resources=AsyncMock(
            return_value={"success": True, "messages": "success"}
        )
    )
    executor = _executor(
        data_store=SimpleNamespace(**{store_method: AsyncMock(return_value=failure)}),
        cache_store=cache_store,
    )

    assert await getattr(executor, method_name)(payload) == failure
    cache_store.delete_resources.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        (
            "list_enabled_cron_tasks",
            "list_enabled_cron_tasks",
        ),
        ("list_cron_tasks", "list_cron_tasks"),
        ("get_cron_task", "get_cron_task"),
    ],
)
async def test_cron_readers_preserve_native_configs(
    method_name,
    store_method,
):
    records = [
        {"task_id": "dict", "extra_config": {"retry": 1}},
        {"task_id": "list", "extra_config": ["one"]},
        {"task_id": "valid", "extra_config": {"retry": 2}},
        {"task_id": "invalid", "extra_config": {}},
        {"task_id": "empty", "extra_config": None},
    ]
    handler = AsyncMock(return_value={"success": True, "messages": records})
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    result = await getattr(executor, method_name)({})

    assert result["messages"][0]["extra_config"] == {"retry": 1}
    assert result["messages"][1]["extra_config"] == ["one"]
    assert result["messages"][2]["extra_config"] == {"retry": 2}
    assert result["messages"][3]["extra_config"] == {}
    assert result["messages"][4]["extra_config"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method"),
    [
        (
            "list_enabled_cron_tasks",
            "list_enabled_cron_tasks",
        ),
        ("list_cron_tasks", "list_cron_tasks"),
        ("get_cron_task", "get_cron_task"),
    ],
)
async def test_cron_readers_return_store_failure_and_exception(
    method_name,
    store_method,
):
    failure = {"success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "success": False,
        "messages": "fail: query crashed",
    }
