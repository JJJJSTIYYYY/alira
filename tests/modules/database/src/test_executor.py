"""Comprehensive tests for the store execution-layer orchestration."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from alira.modules.database.src import executor as execute_module
from alira.modules.database.src.executor import StoreExecutor
from alira.modules.database.src.services.cache import CacheService
from alira.modules.agent.src.shared.message import AliraMessageExtension, AliraUserMessage

_PARSED_RECORD = {"message_uid": "parsed-1", "role": "user", "content": []}


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
        "content": [{"type": "text", "text": f"{node_id}:{cursor}"}],
        "message_uid": f"{node_id}:{cursor}",
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
    rows[2]["content"] = [{"type": "text", "text": "null"}]
    rows[2]["item"] = '{"id":"item-1"}'
    cache = CacheService()
    await cache.set_messages({**payload, "messages": rows})
    cached_rows = (await cache.get_messages(payload))["messages"]

    result = await _executor(cache_store=cache).list_user_face_messages(payload)

    assert result["success"]
    assert [row.node_id for row in result["messages"]["messages"]] == [
        "node-a",
        "node-c",
    ]
    assert result["messages"]["messages"][0].metadata == {}
    assert result["messages"]["messages"][0].extensions == {"source": "json"}
    assert result["messages"]["messages"][1].metadata == {"score": 1}
    assert result["messages"]["messages"][1].extensions == {}
    assert result["messages"]["messages"][1].content[0].text == "null"
    assert result["messages"]["messages"][1].item.id == "item-1"
    assert result["messages"]["current_chain"] == ["node-a", "node-c"]
    assert result["messages"]["branches"]["-"] == [
        {"node_id": "node-a", "cursor": 1},
        {"node_id": "node-b", "cursor": 2},
    ]
    assert (await cache.get_messages(payload))["messages"] == cached_rows
    assert rows[0]["metadata"] == "invalid-json"
    assert rows[2]["item"] == '{"id":"item-1"}'


@pytest.mark.asyncio
async def test_cached_branch_copies_only_selected_records_and_isolates_nested_values():
    copied = []

    class TrackedRecord(dict):
        def __deepcopy__(self, memo):
            copied.append(self["message_uid"])
            return deepcopy(dict(self), memo)

    rows = [
        TrackedRecord(
            _row(
                f"n{index}",
                f"n{index - 1}" if index else "-",
                index + 1,
                "user",
                metadata={"nested": {"value": index}},
            )
        )
        for index in range(200)
    ]
    payload = {"user_uid": "user-1", "conversation_uid": "conversation-1"}
    cache = CacheService()
    await cache.set_messages({**payload, "messages": rows}, take_ownership=True)

    result = await _executor(cache_store=cache).list_messages(
        {**payload, "current_node_id": "n2"}
    )

    assert result["success"]
    assert copied == ["n0:1", "n1:2", "n2:3"]
    assert len(result["messages"]["messages"]) == 3
    first = result["messages"]["messages"][0]
    first.metadata["nested"]["value"] = "changed"
    first.content[0].origin.value["text"] = "changed origin"
    assert rows[0]["metadata"]["nested"]["value"] == 0
    assert rows[0]["content"][0]["text"] == "n0:1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_method", "service_name", "store_method"),
    [
        ("verify_user", "data_store", "verify_user"),
        ("list_all_user", "data_store", "list_all_user"),
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
            "get_branch_chain",
            "cache_store",
            "get_branch_chain",
        ),
        (
            "list_compressed_contexts",
            "data_store",
            "list_compressed_contexts",
        ),
        (
            "create_compressed_context",
            "data_store",
            "create_compressed_context",
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
    messages = [] if executor_method == "list_compressed_contexts" else "ok"
    expected = {"code": 0, "success": True, "messages": messages}
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
        ("list_all_user", "data_store", "list_all_user"),
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
            "list_compressed_contexts",
            "data_store",
            "list_compressed_contexts",
        ),
        (
            "create_compressed_context",
            "data_store",
            "create_compressed_context",
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
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
    }


@pytest.mark.asyncio
async def test_create_conversation_checks_user_then_creates():
    payload = {"user_uid": "user-1"}
    created = {"code": 0, "success": True, "messages": {"conversation_uid": "conv-1"}}
    verify = AsyncMock(return_value={"code": 0, "success": True, "messages": payload})
    create = AsyncMock(return_value=created)
    executor = _executor(
        data_store=SimpleNamespace(verify_user=verify, create_conversation=create)
    )

    assert await executor.create_conversation(payload) == created
    verify.assert_awaited_once_with(payload)
    create.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_create_conversation_short_circuits_and_normalizes_exception():
    failure = {"code": 2001, "success": False, "messages": "User not found."}
    verify = AsyncMock(return_value=failure)
    create = AsyncMock()
    executor = _executor(
        data_store=SimpleNamespace(verify_user=verify, create_conversation=create)
    )

    assert await executor.create_conversation({"user_uid": "user-1"}) == failure
    create.assert_not_awaited()

    verify.side_effect = RuntimeError("lookup failed")
    assert await executor.create_conversation({"user_uid": "user-1"}) == {
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
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
        "code": 0 if write_succeeds else 5000,
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
        return {"code": 5000, "success": False, "messages": "cache unavailable"}

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
        delete_messages=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        )
    )
    data_store = SimpleNamespace(
        update_conversation=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
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
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("as_object", [False, True])
async def test_append_message_persists_and_caches_a_private_snapshot(as_object):
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
    if as_object:
        payload["message"] = AliraUserMessage.from_record(payload["message"])
    original = deepcopy(payload)
    original_record = (
        original["message"].to_record() if as_object else original["message"]
    )
    persisted = []

    async def persist(write_payload):
        persisted.append(deepcopy(write_payload))
        return {
            "code": 0,
            "success": True,
            "messages": {"msg_cursor": 3, "timestamp": "2026-01-01T00:00:00"},
        }

    data_store = SimpleNamespace(append_message=AsyncMock(side_effect=persist))
    cache_store = SimpleNamespace(
        append_message=AsyncMock(return_value={"code": 0, "success": True}),
        update_branch_chain=AsyncMock(return_value={"code": 0, "success": True}),
    )
    executor = _executor(data_store=data_store, cache_store=cache_store)

    assert await executor.append_message(payload) == {
        "code": 0,
        "success": True,
        "messages": {},
    }
    assert payload == original
    assert persisted == [{**original, "message": original_record}]
    write_payload = data_store.append_message.await_args.args[0]
    cached_payload = cache_store.append_message.await_args.args[0]
    assert cached_payload is write_payload
    assert cached_payload is not payload
    assert cached_payload["message"] is not payload["message"]
    assert cached_payload["message"] == {
        **original_record,
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
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
@pytest.mark.parametrize("as_object", [False, True])
async def test_append_message_keeps_database_and_cache_snapshot_consistent_across_await(
    as_object,
):
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "original"}],
        },
    }
    if as_object:
        payload["message"] = AliraUserMessage.from_record(payload["message"])
    entered = asyncio.Event()
    release = asyncio.Event()

    async def persist(write_payload):
        assert write_payload["message"]["content"] == [
            {"type": "text", "text": "original"}
        ]
        entered.set()
        await release.wait()
        assert write_payload["message"]["content"] == [
            {"type": "text", "text": "original"}
        ]
        return {"code": 0, "success": True, "messages": {"msg_cursor": 1}}

    cache_store = SimpleNamespace(
        append_message=AsyncMock(return_value={"code": 0, "success": True})
    )
    executor = _executor(
        data_store=SimpleNamespace(append_message=AsyncMock(side_effect=persist)),
        cache_store=cache_store,
    )
    task = asyncio.create_task(executor.append_message(payload))
    await entered.wait()
    if as_object:
        payload["message"].content[0].text = "changed"
    else:
        payload["message"]["content"][0]["text"] = "changed"
    release.set()
    assert (await task)["success"] is True
    assert cache_store.append_message.await_args.args[0]["message"]["content"] == [
        {"type": "text", "text": "original"}
    ]


@pytest.mark.asyncio
async def test_append_message_short_circuits_and_ignores_cache_append_error():
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conversation-1",
        "message": {},
    }
    failure = {"code": 5000, "success": False, "messages": "write failed"}
    data_store = SimpleNamespace(append_message=AsyncMock(return_value=failure))
    cache_store = SimpleNamespace(
        append_message=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
        update_branch_chain=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.append_message(deepcopy(payload)) == failure
    cache_store.append_message.assert_not_awaited()

    data_store.append_message.return_value = {
        "code": 0,
        "success": True,
        "messages": {"msg_cursor": 1},
    }
    cache_store.append_message.return_value = {
        "code": 5000,
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
                return_value={"code": 0, "success": True, "messages": {}}
            )
        ),
        cache_store=SimpleNamespace(),
    )

    assert await executor.append_message({}) == {
        "code": 1001,
        "success": False,
        "messages": "Invalid request payload.",
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
                "code": 0,
                "success": True,
                "messages": [
                    {"message_uid": ""},
                    {"other": "missing"},
                    {"message_uid": "memory-1"},
                ],
            }
        ),
        delete_compressed_contexts=AsyncMock(return_value={"code": 0, "success": True}),
    )
    cache_store = SimpleNamespace(
        delete_messages=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
        delete_resources=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.delete_messages(payload) == {
        "code": 0,
        "success": True,
        "messages": {},
    }
    cache_store.delete_messages.assert_awaited_once_with(payload)
    data_store.delete_compressed_contexts.assert_awaited_once_with(
        {
            "user_uid": "user-1",
            "conversation_uid": "conversation-1",
            "memory_ids": ["memory-1"],
        }
    )
    cache_store.delete_resources.assert_awaited_once_with(
        {
            "cache_group": "compressed-context:user-1",
            "cache_key": "conversation-1",
        }
    )


@pytest.mark.asyncio
async def test_delete_messages_handles_cache_store_and_memory_failures():
    payload = {"user_uid": "user-1"}
    delete_messages = AsyncMock(
        return_value={"code": 0, "success": True, "messages": None}
    )
    delete_memory = AsyncMock()
    cache_store = SimpleNamespace(
        delete_messages=AsyncMock(
            return_value={"code": 5000, "success": False, "messages": "cache down"}
        ),
        delete_resources=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    data_store = SimpleNamespace(
        delete_messages=delete_messages,
        delete_compressed_contexts=delete_memory,
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert (await executor.delete_messages(payload))["success"] is True
    delete_memory.assert_not_awaited()

    failure = {"code": 5000, "success": False, "messages": "delete failed"}
    delete_messages.return_value = failure
    assert await executor.delete_messages(payload) == failure
    assert cache_store.delete_messages.await_count == 1

    delete_messages.return_value = {
        "code": 0,
        "success": True,
        "messages": [{"message_uid": "memory-1"}],
    }
    memory_failure = {"code": 5000, "success": False, "messages": "memory failed"}
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
                return_value={"code": 0, "success": True, "messages": {}}
            )
        ),
    )

    assert await executor.delete_messages({}) == {
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected_roles", "expected_guess"),
    [
        (
            "list_messages",
            ("user", "assistant", "tool"),
            False,
        ),
        ("list_user_face_messages", ("user", "assistant", "tool"), True),
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
                "code": 0,
                "success": True,
                "cache_hit": True,
                "messages": ["-", "cached-leaf"],
            }
        ),
        get_messages=AsyncMock(
            return_value={
                "code": 0,
                "success": True,
                "cache_hit": True,
                "messages": [{"raw": True}],
            }
        ),
        set_branch_chain=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    executor = _executor(cache_store=cache_store)
    message_nodes.build_visible_messages = Mock(
        return_value=(
            [_PARSED_RECORD],
            {"-": [{"node_id": "branch"}]},
            ["-", "cached-leaf"],
        )
    )

    result = await getattr(executor, method_name)(payload)

    assert result["messages"]["messages"] == [
        AliraMessageExtension.from_record(_PARSED_RECORD)
    ]
    assert result["messages"]["branches"] == {"-": [{"node_id": "branch"}]}
    assert "node_id_chain" not in payload
    assert result["messages"]["current_chain"] == ["-", "cached-leaf"]
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
            {
                "code": 5000,
                "success": False,
                "cache_hit": True,
                "messages": ["ignored"],
            },
        ),
        (
            "list_messages",
            {"code": 0, "success": True, "cache_hit": True, "messages": []},
        ),
        (
            "list_user_face_messages",
            {
                "code": 5000,
                "success": False,
                "cache_hit": True,
                "messages": ["ignored"],
            },
        ),
        (
            "list_user_face_messages",
            {"code": 0, "success": True, "cache_hit": True, "messages": []},
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
                "code": 0,
                "success": True,
                "cache_hit": True,
                "messages": [{"raw": True}],
            }
        ),
        set_branch_chain=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    executor = _executor(cache_store=cache_store)
    message_nodes.build_visible_messages = Mock(return_value=([_PARSED_RECORD], {}, []))

    result = await getattr(executor, method_name)(payload)

    assert result["messages"]["messages"] == [
        AliraMessageExtension.from_record(_PARSED_RECORD)
    ]
    assert message_nodes.build_visible_messages.call_args.args[0] == "-"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["list_messages", "list_user_face_messages"],
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
            return_value={
                "code": 5000,
                "success": False,
                "messages": "chain cache down",
            }
        ),
        get_messages=AsyncMock(
            return_value={"code": 0, "success": True, "cache_hit": False}
        ),
        set_messages=AsyncMock(
            return_value={
                "code": 5000,
                "success": False,
                "messages": "message cache down",
            }
        ),
        set_branch_chain=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    data_store = SimpleNamespace(
        list_messages=AsyncMock(
            return_value={
                "code": 0,
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
            [_PARSED_RECORD],
            {},
            ["node-1"],
        )
    )

    result = await getattr(executor, method_name)(payload)

    assert result["messages"]["messages"] == [
        AliraMessageExtension.from_record(_PARSED_RECORD)
    ]
    assert "node_id_chain" not in payload
    assert result["messages"]["current_chain"] == ["node-1"]
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
        },
        take_ownership=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["list_messages", "list_user_face_messages"],
)
async def test_list_messages_returns_database_failure_or_empty_result(
    method_name,
):
    payload = {"current_node_id": "known"}
    cache_store = SimpleNamespace(
        get_messages=AsyncMock(
            return_value={"code": 5000, "success": False, "cache_hit": False}
        )
    )
    failure = {"code": 5000, "success": False, "messages": "db failed"}
    data_store = SimpleNamespace(list_messages=AsyncMock(return_value=failure))
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await getattr(executor, method_name)(payload.copy()) == failure

    empty = {"code": 0, "success": True, "messages": []}
    data_store.list_messages.return_value = empty
    cache_store.get_messages.return_value = {
        "code": 0,
        "success": True,
        "cache_hit": False,
    }
    assert await getattr(executor, method_name)(payload.copy()) == {
        "success": True,
        "code": 0,
        "messages": {"messages": [], "branches": {}, "current_chain": []},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["list_messages", "list_user_face_messages"])
async def test_message_readers_normalize_branch_assembly_exception(
    method_name, message_nodes
):
    executor = _executor(
        cache_store=SimpleNamespace(
            get_messages=AsyncMock(
                return_value={
                    "code": 0,
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
        "code": 1001,
        "success": False,
        "messages": "Invalid request payload.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "id_key"),
    [
        ("create_llm_provider", "create_llm_provider", "provider_id"),
        ("create_mcp_server", "create_mcp_server", "mcp_id"),
        ("create_cron_task", "create_cron_task", "task_id"),
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
    handler = AsyncMock(
        return_value={"code": 0, "success": True, "messages": "created"}
    )
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
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
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
                    "cache_key": "all",
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
            ({"cache_group": "mcp"},),
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
        return {"code": 0, "success": True, "messages": "created"}

    async def invalidate(payload):
        call_order.append(("cache", payload))
        return {"code": 0, "success": True, "messages": {}}

    executor = _executor(
        data_store=SimpleNamespace(**{store_method: AsyncMock(side_effect=persist)}),
        cache_store=SimpleNamespace(delete_resources=AsyncMock(side_effect=invalidate)),
    )
    payload = {}

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
    handler = AsyncMock(return_value={"code": 0, "success": True, "messages": records})
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
    failure = {"code": 5000, "success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
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
    handler = AsyncMock(return_value={"code": 0, "success": True, "messages": records})
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
    failure = {"code": 5000, "success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "payload", "store_method", "expected_cache"),
    [
        (
            "list_llm_providers",
            {},
            "list_llm_providers",
            {
                "cache_group": "providers:list",
                "cache_key": "all",
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
            {},
            "list_mcp_servers",
            {
                "cache_group": "mcp",
                "cache_key": "all",
            },
        ),
        (
            "list_enabled_mcp_servers",
            {},
            "list_enabled_mcp_servers",
            {
                "cache_group": "mcp",
                "cache_key": "enabled",
            },
        ),
        (
            "list_compressed_contexts",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
            },
            "list_compressed_contexts",
            {
                "cache_group": "compressed-context:user-1",
                "cache_key": "conversation-1",
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
                "code": 0,
                "success": True,
                "messages": cached_messages,
                "cache_hit": True,
            }
        ),
        set_resource=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    data_reader = AsyncMock()
    executor = _executor(
        cache_store=cache_store,
        data_store=SimpleNamespace(**{store_method: data_reader}),
    )

    assert await getattr(executor, method_name)(payload) == {
        "code": 0,
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
            "list_llm_providers",
            {},
            "list_llm_providers",
            [{"provider_id": "provider-1", "model_list": ["model-a"]}],
            [{"provider_id": "provider-1", "model_list": ["model-a"]}],
            {
                "cache_group": "providers:list",
                "cache_key": "all",
            },
        ),
        (
            "list_mcp_servers",
            {},
            "list_mcp_servers",
            [{"mcp_id": "mcp-1", "config": {"command": "demo"}}],
            [{"mcp_id": "mcp-1", "config": {"command": "demo"}}],
            {
                "cache_group": "mcp",
                "cache_key": "all",
            },
        ),
        (
            "list_compressed_contexts",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
            },
            "list_compressed_contexts",
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
                "cache_group": "compressed-context:user-1",
                "cache_key": "conversation-1",
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
                "code": 0,
                "success": True,
                "messages": [],
                "cache_hit": False,
            }
        ),
        set_resource=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
        ),
    )
    data_reader = AsyncMock(
        return_value={
            "code": 0,
            "success": True,
            "messages": deepcopy(database_messages),
        }
    )
    executor = _executor(
        cache_store=cache_store,
        data_store=SimpleNamespace(**{store_method: data_reader}),
    )

    result = await getattr(executor, method_name)(payload)

    assert result == {"code": 0, "success": True, "messages": expected_messages}
    data_reader.assert_awaited_once_with(payload)
    cache_store.set_resource.assert_awaited_once_with(
        {
            **expected_cache,
            "messages": expected_messages,
        }
    )


@pytest.mark.asyncio
async def test_resource_read_falls_back_and_ignores_cache_failures():
    payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
    data_store = SimpleNamespace(
        list_compressed_contexts=AsyncMock(
            return_value={
                "code": 0,
                "success": True,
                "messages": [{"memory_id": "memory-1"}],
            }
        )
    )
    cache_store = SimpleNamespace(
        get_resource=AsyncMock(
            return_value={"code": 5000, "success": False, "messages": "cache read down"}
        ),
        set_resource=AsyncMock(
            return_value={
                "code": 5000,
                "success": False,
                "messages": "cache write down",
            }
        ),
    )
    executor = _executor(
        data_store=data_store,
        cache_store=cache_store,
    )

    assert await executor.list_compressed_contexts(payload) == {
        "code": 0,
        "success": True,
        "messages": [{"memory_id": "memory-1"}],
    }
    data_store.list_compressed_contexts.assert_awaited_once_with(payload)

    cache_store.get_resource.side_effect = None
    cache_store.get_resource.return_value = {
        "code": 5000,
        "success": False,
        "messages": "cache unavailable",
        "cache_hit": False,
    }
    cache_store.set_resource.return_value = {
        "code": 5000,
        "success": False,
        "messages": "cache write down",
    }
    assert (await executor.list_compressed_contexts(payload))["success"] is True
    assert data_store.list_compressed_contexts.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "store_method", "payload", "expected_targets"),
    [
        (
            "update_llm_provider",
            "update_llm_provider",
            {"provider_id": "provider-1"},
            (
                {
                    "cache_group": "providers:list",
                    "cache_key": "all",
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
            {"mcp_id": "mcp-1"},
            ({"cache_group": "mcp"},),
        ),
        (
            "create_compressed_context",
            "create_compressed_context",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
                "memory_id": "message-1",
                "content": "Recent context",
            },
            (
                {
                    "cache_group": "compressed-context:user-1",
                    "cache_key": "conversation-1",
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
        return {"code": 0, "success": True, "messages": {}}

    async def invalidate(target):
        call_order.append(("cache", target))
        return {"code": 5000, "success": False, "messages": "cache down"}

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
            "update_llm_provider",
            "update_llm_provider",
            {"provider_id": "provider-1"},
        ),
        (
            "update_mcp_server",
            "update_mcp_server",
            {"mcp_id": "mcp-1"},
        ),
        (
            "create_compressed_context",
            "create_compressed_context",
            {
                "user_uid": "user-1",
                "conversation_uid": "conversation-1",
                "memory_id": "message-1",
                "content": "Recent context",
            },
        ),
    ],
)
async def test_failed_resource_mutations_do_not_invalidate_cache(
    method_name,
    store_method,
    payload,
):
    failure = {"code": 5000, "success": False, "messages": "database write failed"}
    cache_store = SimpleNamespace(
        delete_resources=AsyncMock(
            return_value={"code": 0, "success": True, "messages": {}}
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
    handler = AsyncMock(return_value={"code": 0, "success": True, "messages": records})
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
    failure = {"code": 5000, "success": False, "messages": "query failed"}
    handler = AsyncMock(return_value=failure)
    executor = _executor(data_store=SimpleNamespace(**{store_method: handler}))

    assert await getattr(executor, method_name)({}) == failure

    handler.side_effect = RuntimeError("query crashed")
    assert await getattr(executor, method_name)({}) == {
        "code": 5000,
        "success": False,
        "messages": "Internal database service error.",
    }


@pytest.mark.parametrize(
    ("action", "data_method"),
    [
        ("create_conversation", "verify_user"),
        ("upload_files", "verify_user"),
        ("create_skills", "verify_user"),
        ("append_message", "append_message"),
        ("delete_messages", "delete_messages"),
        ("list_llm_providers", "list_llm_providers"),
        ("create_llm_provider", "create_llm_provider"),
        ("search_messages", "search_messages"),
    ],
)
async def test_business_errors_stop_orchestration_before_writes_or_cache_changes(
    action, data_method
):
    rejection = {"success": True, "code": 2001, "messages": "User not found."}
    data = SimpleNamespace(**{data_method: AsyncMock(return_value=rejection)})
    cache = SimpleNamespace(
        **{
            name: AsyncMock()
            for name in (
                "append_message",
                "delete_messages",
                "set_resource",
                "delete_resources",
            )
        }
    )
    executor = _executor(data_store=data, cache_store=cache)
    payload = {"user_uid": "missing", "message": {"role": "user", "content": []}}

    assert await getattr(executor, action)(payload) == rejection
    for name in (
        "append_message",
        "delete_messages",
        "set_resource",
        "delete_resources",
    ):
        getattr(cache, name).assert_not_awaited()
