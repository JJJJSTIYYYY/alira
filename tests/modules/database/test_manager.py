"""Cross-thread execution, persistence, cancellation, and lifecycle boundaries."""

import asyncio
import gc
import threading
from uuid import UUID

import pytest
from sqlalchemy import text

import alira.modules.database as store_package
from alira.lifespan.auto_init import auto_init
from alira.modules.database import StoreManager, query_store
from alira.modules.database import manager as manager_module
from alira.modules.database import runtime as runtime_module
from alira.modules.database.executor import StoreExecutor
from alira.modules.database.services.cache import CacheService
from alira.modules.database.services.data import DataService
from alira.modules.database.utils.errors import StoreError
from alira.modules.database.utils.message_tree import MessageNodeTree


async def wait_for_thread(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), timeout=4)


async def wait_in_store(event):
    deadline = asyncio.get_running_loop().time() + 3
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("test store gate timed out")
        await asyncio.sleep(0.001)


@pytest.fixture
async def manager(tmp_path):
    instance = StoreManager(
        db_path=tmp_path / "store.sqlite3",
        cache_path=tmp_path / "cache.json",
        worker_count=1,
    )
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=5)


def extra_actions(monkeypatch, **actions):
    original = StoreExecutor.export_handlers
    monkeypatch.setattr(
        StoreExecutor,
        "export_handlers",
        lambda executor: {**original(executor), **actions},
    )


async def create_conversation(manager):
    user = await manager.query("create_user", {"user_name": "alice"})
    user_uid = user["messages"]["user_uid"]
    created = await manager.query("create_conversation", {"user_uid": user_uid})
    return {
        "user_uid": user_uid,
        "conversation_uid": created["messages"]["conversation_uid"],
    }


def test_public_surface_and_import_safe_lifecycle_owner():
    assert store_package.__all__ == ["StoreManager", "query_store"]
    assert manager_module._store_manager in auto_init._services
    assert manager_module._store_manager._state is None
    assert not any(
        isinstance(service, (CacheService, DataService))
        for service in auto_init._services
    )
    instance = StoreManager(db_path=":memory:")
    assert instance._state is None
    with pytest.raises(ValueError, match="greater than zero"):
        StoreManager(worker_count=0)


@pytest.mark.parametrize("worker_count", [1, 2])
@pytest.mark.parametrize("as_object", [False, True])
async def test_query_store_round_trip_cache_isolation_and_restart(
    tmp_path, monkeypatch, worker_count, as_object
):
    manager = StoreManager(
        db_path=tmp_path / "store.sqlite3",
        cache_path=tmp_path / "cache.json",
        worker_count=worker_count,
    )
    monkeypatch.setattr(manager_module, "_store_manager", manager)
    try:
        payload = await create_conversation(manager)
        assert UUID(payload["conversation_uid"]).version == 4
        message = {
            "message_uid": "message-1",
            "node_id": "node-1",
            "parent_id": "-",
            "role": "assistant",
            "content": [{"type": "text", "text": "null"}],
            "metadata": {"usage": {"total": 2}},
        }
        if as_object:
            from alira.shared.models.message import AliraAssistantMessage

            message = AliraAssistantMessage.from_record(message)
        await query_store("append_message", {**payload, "message": message})
        assert "msg_cursor" not in message
        assert not hasattr(message, "msg_cursor")
        request = {**payload, "current_node_id": "node-1"}
        first = await query_store("list_messages", request)
        assert not first.get("cache_hit", False)
        assert first["messages"]["next_cursor"] == 2
        cached = await query_store("list_messages", request)
        assert set(cached) == {"success", "code", "messages"}
        assert cached["messages"]["cache_hit"]
        assert cached["messages"]["messages"] == first["messages"]["messages"]
        assert cached["messages"]["messages"][0].message_uid == "message-1"
        assert all(
            not hasattr(cached["messages"]["messages"][0], key)
            for key in (
                "msg_cursor",
                "timestamp",
                "is_deleted",
                "id",
                "conversation_id",
            )
        )
        cached["messages"]["messages"][0].metadata["usage"]["total"] = 999
        state = manager._state
        await manager.stop()
        assert not state.thread.is_alive()
        assert state.loop.is_closed()
        assert manager._state is None
        assert (tmp_path / "cache.json").exists()
        restored = await query_store("list_messages", request)
        assert set(restored) == {"success", "code", "messages"}
        assert restored["messages"]["cache_hit"]
        assert restored["messages"]["messages"] == first["messages"]["messages"]
        assert manager._state is not state
        assert (await query_store("list_conversations", payload))["messages"]
    finally:
        await manager.stop()


async def test_services_and_actions_are_created_started_and_closed_on_store_thread(
    manager, monkeypatch
):
    records = []
    caller_thread = threading.get_ident()
    caller_loop = asyncio.get_running_loop()

    class TrackedCache(CacheService):
        def __init__(self, *args):
            records.append(
                ("cache-init", threading.get_ident(), asyncio.get_running_loop())
            )
            super().__init__(*args)

        async def start(self):
            records.append(
                ("cache-start", threading.get_ident(), asyncio.get_running_loop())
            )
            await super().start()

        async def stop(self):
            records.append(
                ("cache-stop", threading.get_ident(), asyncio.get_running_loop())
            )
            await super().stop()

    class TrackedData(DataService):
        def __init__(self, *args):
            records.append(
                ("data-init", threading.get_ident(), asyncio.get_running_loop())
            )
            super().__init__(*args)

        async def start(self):
            records.append(
                ("data-start", threading.get_ident(), asyncio.get_running_loop())
            )
            await super().start()

        async def stop(self):
            records.append(
                ("data-stop", threading.get_ident(), asyncio.get_running_loop())
            )
            await super().stop()
            assert self._engine is None

    async def location(payload):
        records.append(("action", threading.get_ident(), asyncio.get_running_loop()))
        return {"code": 0, "success": True, "messages": "located"}

    monkeypatch.setattr(runtime_module, "CacheService", TrackedCache)
    monkeypatch.setattr(runtime_module, "DataService", TrackedData)
    extra_actions(monkeypatch, location=location)
    assert (await manager.query("location", {}))["messages"] == "located"
    state = manager._state
    await manager.stop()
    assert {record[1] for record in records} == {state.thread.ident}
    assert all(
        record[1] != caller_thread and record[2] is not caller_loop
        for record in records
    )
    assert [record[0] for record in records] == [
        "cache-init",
        "data-init",
        "cache-start",
        "data-start",
        "action",
        "data-stop",
        "cache-stop",
    ]


async def test_concurrent_start_and_stop_share_a_single_thread(manager):
    await asyncio.gather(*(manager.start() for _ in range(8)))
    state = manager._state
    assert state.thread.is_alive()
    assert len(state.runtime._workers) == 1
    await asyncio.gather(*(manager.start() for _ in range(8)))
    assert manager._state is state
    await asyncio.gather(*(manager.stop() for _ in range(8)))
    assert not state.thread.is_alive()
    await manager.stop()


async def test_synchronous_message_work_does_not_block_the_caller_loop(
    manager, monkeypatch
):
    payload = await create_conversation(manager)
    await manager.query(
        "append_message",
        {
            **payload,
            "message": {
                "message_uid": "m1",
                "node_id": "n1",
                "parent_id": "-",
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
    )
    entered, release = threading.Event(), threading.Event()
    original = MessageNodeTree.build_visible_messages
    executed_on = []

    def blocked_projection(helper, *args, **kwargs):
        executed_on.append(threading.get_ident())
        entered.set()
        if not release.wait(timeout=3):
            raise TimeoutError("projection gate timed out")
        return original(helper, *args, **kwargs)

    monkeypatch.setattr(MessageNodeTree, "build_visible_messages", blocked_projection)
    query = asyncio.create_task(
        manager.query("list_messages", {**payload, "current_node_id": "n1"})
    )
    try:
        await wait_for_thread(entered)
        # This callback must run while the store's synchronous code is blocked.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.5)
        assert not query.done()
        assert executed_on == [manager._state.thread.ident]
        assert executed_on[0] != threading.get_ident()
    finally:
        release.set()
    assert (await query)["messages"]["messages"][0]["content"] == [
        {"type": "text", "text": "hello"}
    ]


async def test_one_manager_accepts_queries_from_distinct_caller_event_loops(manager):
    await create_conversation(manager)

    def other_loop():
        return asyncio.run(manager.query("verify_user", {"user_name": "alice"}))

    results = await asyncio.gather(
        manager.query("verify_user", {"user_name": "alice"}),
        asyncio.to_thread(other_loop),
        asyncio.to_thread(other_loop),
    )
    assert all(result["success"] for result in results)


@pytest.mark.parametrize("worker_count", [1, 2])
async def test_timeout_abandons_queued_work_without_losing_workers(
    tmp_path, monkeypatch, worker_count
):
    entered, release = threading.Event(), threading.Event()
    seen = []

    async def blocked(payload):
        seen.append(payload["id"])
        if payload["id"] == "active":
            entered.set()
            await wait_in_store(release)
        return {"code": 0, "success": True, "messages": payload["id"]}

    extra_actions(monkeypatch, blocked=blocked)
    manager = StoreManager(
        db_path=":memory:",
        cache_path=tmp_path / "cache.json",
        worker_count=worker_count,
    )
    active = asyncio.create_task(manager.query("blocked", {"id": "active"}))
    try:
        await wait_for_thread(entered)
        if worker_count == 1:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    manager.query("blocked", {"id": "abandoned"}), timeout=0.02
                )
        else:
            assert (await manager.query("blocked", {"id": "concurrent"}))[
                "messages"
            ] == "concurrent"
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        release.set()
        if not active.cancelled():
            assert (await active)["messages"] == "active"
        assert (await manager.query("blocked", {"id": "next"}))["messages"] == "next"
        assert "abandoned" not in seen
    finally:
        release.set()
        await manager.stop()


async def test_embedding_sync_survives_request_timeout_and_reuses_one_store_task(
    tmp_path, monkeypatch
):
    import struct

    pytest.importorskip("sqlite_vec")
    from alira.modules.database.services.data import service as data_module
    from alira.modules.database.services.data.embedding import MessageEmbedding

    manager = StoreManager(
        db_path=tmp_path / "store.sqlite3",
        cache_path=tmp_path / "cache.json",
        worker_count=1,
    )
    monkeypatch.setattr(manager_module, "_store_manager", manager)
    monkeypatch.setattr(data_module, "SQLITE_EMBEDDING_ENABLE", False)
    entered, release = threading.Event(), threading.Event()
    encoded = []

    async def blocked_encode(self, texts, *, query=False):
        encoded.append(texts)
        entered.set()
        await wait_in_store(release)
        return [struct.pack("2f", 1.0, 0.0) for _ in texts]

    responses = []
    response_ready = asyncio.Event()

    async def request_handler():
        responses.append(await query_store("sync_messages_embedding", {}))
        response_ready.set()
        # The request times out before its prepared response reaches the client.
        await asyncio.Event().wait()

    async def wait_completed():
        while True:
            result = await query_store("get_messages_embedding_sync_status", {})
            if result["messages"]["status"] != "running":
                return result["messages"]
            await asyncio.sleep(0.001)

    request = None
    try:
        identity = await create_conversation(manager)
        await query_store(
            "append_message",
            {
                **identity,
                "message": {
                    "message_uid": "history",
                    "node_id": "n1",
                    "role": "user",
                    "content": [{"type": "text", "text": "history body"}],
                },
            },
        )
        await manager.stop()
        model_path = tmp_path / "model"
        model_path.mkdir()
        monkeypatch.setattr(data_module, "SQLITE_EMBEDDING_ENABLE", True)
        monkeypatch.setattr(data_module, "SQLITE_EMBEDDING_MODEL", str(model_path))
        monkeypatch.setattr(data_module, "VECTOR_DIMENSION", 2)
        monkeypatch.setattr(MessageEmbedding, "encode", blocked_encode)
        request = asyncio.create_task(request_handler())
        await asyncio.wait_for(response_ready.wait(), timeout=2)
        await wait_for_thread(entered)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(request, timeout=0.01)

        # Even with one request worker, an active sync cannot hold up these calls.
        duplicates = await asyncio.wait_for(
            asyncio.gather(
                *(query_store("sync_messages_embedding", {}) for _ in range(20))
            ),
            timeout=2,
        )
        assert all(result == responses[0] for result in duplicates)
        assert encoded == [["history body"]]
        running = await query_store("get_messages_embedding_sync_status", {})
        assert running["messages"]["status"] == "running"
        assert running["messages"]["task_id"] == responses[0]["messages"]["task_id"]
        assert (await query_store("verify_user", {"user_name": "alice"}))["success"]
        release.set()
        completed = await asyncio.wait_for(wait_completed(), timeout=3)
        assert completed["status"] == "completed"
        assert completed["inserted"] == 1
        assert completed["error"] is None
    finally:
        release.set()
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await manager.stop()


@pytest.mark.parametrize("worker_count", [1, 2])
async def test_shutdown_cancels_all_pending_work_then_allows_restart(
    tmp_path, monkeypatch, worker_count
):
    entered = threading.Event()
    cleaning, release = threading.Event(), threading.Event()

    async def blocked(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await wait_in_store(release)
        return {"code": 0, "success": True, "messages": "unreachable"}

    extra_actions(monkeypatch, blocked=blocked)
    manager = StoreManager(
        db_path=":memory:",
        cache_path=tmp_path / "cache.json",
        worker_count=worker_count,
    )
    pending = [
        asyncio.create_task(manager.query("blocked", {}))
        for _ in range(worker_count + 2)
    ]
    stopping = None
    try:
        await wait_for_thread(entered)
        state = manager._state
        stopping = asyncio.create_task(manager.stop())
        await wait_for_thread(cleaning)
        assert await manager.query("list_conversations", {"user_uid": "nobody"}) == {
            "success": False,
            "code": 3004,
            "messages": "Database service is unavailable.",
        }
        with pytest.raises(StoreError, match="unavailable"):
            await manager.start()
        assert not stopping.done()
    finally:
        release.set()
        if stopping is not None:
            await stopping
        await manager.stop()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(
        isinstance(result, (asyncio.CancelledError, RuntimeError)) for result in results
    )
    assert not state.thread.is_alive()
    assert state.runtime._queue.empty()
    assert state.runtime._queue._unfinished_tasks == 0
    assert (await manager.query("list_conversations", {"user_uid": "nobody"}))[
        "success"
    ]
    await manager.stop()


async def test_cancelled_stop_caller_does_not_interrupt_service_cleanup(
    manager, monkeypatch
):
    cleaning, release = threading.Event(), threading.Event()
    original = DataService.stop

    async def delayed_stop(service):
        cleaning.set()
        await wait_in_store(release)
        await original(service)

    monkeypatch.setattr(DataService, "stop", delayed_stop)
    await manager.start()
    state = manager._state
    caller = asyncio.create_task(manager.stop())
    try:
        await wait_for_thread(cleaning)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not state.finished.done()
        assert state.thread.is_alive()
    finally:
        release.set()
        await manager.stop()
    assert not state.thread.is_alive()


async def test_stop_during_startup_waits_for_cleanup_and_can_retry(
    manager, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = DataService.start

    async def delayed_start(service):
        entered.set()
        await wait_in_store(release)
        await original(service)

    monkeypatch.setattr(DataService, "start", delayed_start)
    starting = asyncio.create_task(manager.start())
    await wait_for_thread(entered)
    state = manager._state
    stopping = asyncio.create_task(manager.stop())
    try:
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
        await stopping
    with pytest.raises(StoreError, match="unavailable"):
        await starting
    assert not state.thread.is_alive()
    await manager.start()


async def test_cancelled_start_waiter_does_not_cancel_other_start_waiters(
    manager, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = DataService.start

    async def delayed_start(service):
        entered.set()
        await wait_in_store(release)
        await original(service)

    monkeypatch.setattr(DataService, "start", delayed_start)
    first = asyncio.create_task(manager.start())
    await wait_for_thread(entered)
    second = asyncio.create_task(manager.start())
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
        await second
    assert manager._state.thread.is_alive()


async def test_startup_failure_closes_started_services_and_allows_retry(
    manager, monkeypatch
):
    cache_closed = []
    original_stop = CacheService.stop

    async def fail_start(service):
        raise RuntimeError("database unavailable")

    async def cache_stop(service):
        await original_stop(service)
        cache_closed.append(threading.get_ident())

    with monkeypatch.context() as patch:
        patch.setattr(DataService, "start", fail_start)
        patch.setattr(CacheService, "stop", cache_stop)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await manager.start()
        state = manager._state
        await manager.stop()
    assert not state.thread.is_alive()
    assert cache_closed == [state.thread.ident]
    assert (await manager.query("list_conversations", {"user_uid": "nobody"}))[
        "success"
    ]


async def test_shutdown_failure_still_closes_database_and_joins_thread(
    manager, monkeypatch
):
    closed = []
    original_data_stop = DataService.stop
    original_cache_stop = CacheService.stop

    async def data_stop(service):
        await original_data_stop(service)
        closed.append(service._engine is None)

    async def cache_stop(service):
        await original_cache_stop(service)
        raise OSError("persistence failure")

    monkeypatch.setattr(DataService, "stop", data_stop)
    monkeypatch.setattr(CacheService, "stop", cache_stop)
    await manager.start()
    state = manager._state
    with pytest.raises(ExceptionGroup, match="Store shutdown failed"):
        await manager.stop()
    assert closed == [True]
    assert not state.thread.is_alive()
    assert state.loop.is_closed()
    assert manager._state is None


async def test_action_errors_and_child_cancellation_do_not_kill_workers(
    manager, monkeypatch
):
    async def explode(payload):
        raise ValueError("action exploded")

    async def cancel(payload):
        raise asyncio.CancelledError

    extra_actions(monkeypatch, explode=explode, cancel=cancel)
    assert await manager.query("does_not_exist", {}) == {
        "success": False,
        "code": 1002,
        "messages": "Unknown database action.",
    }
    assert await manager.query("explode", {}) == {
        "success": False,
        "code": 1001,
        "messages": "Invalid request payload.",
    }
    with pytest.raises(asyncio.CancelledError):
        await manager.query("cancel", {})
    assert (await manager.query("list_conversations", {"user_uid": "nobody"}))[
        "success"
    ]
    assert all(not worker.done() for worker in manager._state.runtime._workers)


@pytest.mark.parametrize("transition", ["start", "stop"])
async def test_failure_after_waiter_cancellation_does_not_leave_unobserved_errors(
    manager, monkeypatch, transition
):
    entered, release = threading.Event(), threading.Event()
    errors = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    original_operation = getattr(DataService, transition)
    if transition == "stop":
        await manager.start()

    async def delayed_failure(service):
        entered.set()
        await wait_in_store(release)
        if transition == "stop":
            await original_operation(service)
        raise RuntimeError("failure after waiter cancellation")

    monkeypatch.setattr(DataService, transition, delayed_failure)
    loop.set_exception_handler(lambda _loop, context: errors.append(context["message"]))
    caller = asyncio.create_task(getattr(manager, transition)())
    try:
        await wait_for_thread(entered)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        if transition == "stop":
            with pytest.raises(ExceptionGroup, match="Store shutdown failed"):
                await manager.stop()
        else:
            await manager.stop()
        del caller
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release.set()
        loop.set_exception_handler(original_handler)


async def test_stop_waits_for_active_driver_io_and_rolls_back_before_restart(
    manager, monkeypatch
):
    entered, release, stopping, cancelled = (threading.Event() for _ in range(4))
    original_export = StoreExecutor.export_handlers
    original_stop = runtime_module._StoreRuntime.stop

    def slow_sql():
        entered.set()
        if not release.wait(timeout=3):
            raise TimeoutError("driver query gate timed out")
        return "cancelled-user"

    def export(executor):
        async def slow_write(payload):
            async with executor.data_store._transaction() as connection:
                await connection.run_sync(
                    lambda sync: sync.connection.create_function(
                        "slow_sql", 0, slow_sql
                    )
                )
            await executor.data_store._execute(
                text(
                    "INSERT INTO users (user_uid, user_name) "
                    "VALUES ('cancelled-user', slow_sql())"
                )
            )
            return {"code": 0, "success": True, "messages": "unreachable"}

        return {**original_export(executor), "slow_write": slow_write}

    async def stop(runtime):
        stopping.set()
        await original_stop(runtime)

    monkeypatch.setattr(StoreExecutor, "export_handlers", export)
    monkeypatch.setattr(runtime_module._StoreRuntime, "stop", stop)
    existing = await manager.query("create_user", {"user_name": "existing"})
    query = asyncio.create_task(manager.query("slow_write", {}))
    shutdown = None
    try:
        await wait_for_thread(entered)
        state = manager._state
        shutdown = asyncio.create_task(manager.stop())
        await wait_for_thread(stopping)

        def check_cancelled_workers():
            if all(worker.cancelling() for worker in state.runtime._workers):
                cancelled.set()

        state.loop.call_soon_threadsafe(check_cancelled_workers)
        await wait_for_thread(cancelled)
        assert not shutdown.done()
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        else:
            await manager.stop()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert not state.thread.is_alive()
    assert (await manager.query("verify_user", {"user_name": "existing"}))["messages"][
        "user_uid"
    ] == existing["messages"]["user_uid"]
    # Reusing the cancelled write's unique ID proves that its transaction rolled back.
    assert (
        await manager.query(
            "create_user",
            {
                "user_uid": "cancelled-user",
                "user_name": "cancelled-user",
            },
        )
    )["success"]


async def test_tool_role_survives_branch_filter_cache_and_restart(manager):
    from my_llm import TextContent, ToolCall
    from my_llm.middleware.protocol import generation_input

    from alira.shared.models.message import (
        AliraAssistantMessage,
        AliraMessageExtension,
        AliraToolMessage,
        AliraUserMessage,
    )

    payload = await create_conversation(manager)
    original = [
        AliraUserMessage(content=[TextContent("question")]),
        AliraAssistantMessage(content=[ToolCall("c1", "lookup", {})]),
        AliraToolMessage(call_id="c1", content=[TextContent("result")]),
        AliraAssistantMessage(content=[TextContent("answer")]),
    ]
    for index, message in enumerate(original):
        message.node_id = f"n{index}"
        message.parent_id = f"n{index - 1}" if index else "-"
        await manager.query("append_message", {**payload, "message": message})
    request = {**payload, "current_node_id": "n3"}
    cold = await manager.query("list_messages", request)
    assert not cold.get("cache_hit", False)
    assert [row["role"] for row in cold["messages"]["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    restored = cold["messages"]["messages"]
    assert all(isinstance(message, AliraMessageExtension) for message in restored)
    assert generation_input(restored, None).messages[2].role == "tool"
    hot = await manager.query("list_messages", request)
    assert hot["messages"]["messages"] == cold["messages"]["messages"]
    await manager.stop()
    restarted = await manager.query("list_messages", request)
    assert restarted["messages"]["messages"] == cold["messages"]["messages"]
    visible = await manager.query("list_user_face_messages", request)
    assert [row["role"] for row in visible["messages"]["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


async def test_full_generation_ids_keep_message_nodes_distinct_after_restart(manager):
    from my_llm import TextContent

    from alira.modules.agent.core.utils.node_id import (
        convert_generation_id_to_message_node_id,
    )
    from alira.shared.models.message import (
        AliraAssistantMessage,
        AliraUserMessage,
    )

    payload = await create_conversation(manager)
    generation_ids = ("0" * 20 + "123456789abc", "1" * 20 + "123456789abc")
    original = []
    parent_id = "-"
    for generation_id in generation_ids:
        for message_type in (
            AliraUserMessage,
            AliraAssistantMessage,
        ):
            message = message_type(
                generation_id=generation_id,
                parent_id=parent_id,
                content=[TextContent(generation_id)],
            )
            message.node_id = convert_generation_id_to_message_node_id(
                generation_id, message.role
            )
            await manager.query("append_message", {**payload, "message": message})
            original.append(message)
            parent_id = message.node_id

    for restart in (False, True):
        if restart:
            await manager.stop()
        for selected, expected in (
            (original[1], original[:2]),
            (original[-1], original),
        ):
            result = await manager.query(
                "list_messages", {**payload, "current_node_id": selected.node_id}
            )
            assert [
                message.to_record() for message in result["messages"]["messages"]
            ] == [{**message.to_record(), **payload} for message in expected]
            assert result["messages"]["current_chain"] == [
                message.node_id for message in expected
            ]
        visible = await manager.query(
            "list_user_face_messages",
            {**payload, "current_node_id": original[-1].node_id},
        )
        assert [message.message_uid for message in visible["messages"]["messages"]] == [
            message.message_uid for message in original
        ]


@pytest.mark.parametrize("as_record", [False, True])
async def test_system_messages_are_rejected_before_persistence(manager, as_record):
    from my_llm import TextContent

    from alira.shared.models.message import AliraSystemMessage

    payload = await create_conversation(manager)
    message = AliraSystemMessage(content=[TextContent("runtime rules")])
    value = (
        {**message.to_wire(), "message_uid": message.message_uid}
        if as_record
        else message
    )
    assert await manager.query("append_message", {**payload, "message": value}) == {
        "success": False,
        "code": 1001,
        "messages": "Invalid request payload.",
    }
    assert (await manager.query("list_messages", payload))["messages"]["messages"] == []
    assert (await manager.query("get_conversation", payload))["messages"][0][
        "latest_cursor"
    ] == 0


@pytest.mark.parametrize("action", ["search_messages", "search_messages_vec"])
async def test_search_returns_business_messages_and_separate_conversation_details(
    manager, action
):
    from my_llm import TextContent

    from alira.shared.models.message import AliraAssistantMessage, AliraUserMessage

    payload = await create_conversation(manager)
    original = [
        AliraUserMessage(
            content=[TextContent("find this phrase")],
            metadata={"source": "user"},
            extensions={"uploaded_files": ["brief.pdf"]},
        ),
        AliraAssistantMessage(
            content=[TextContent("find this answer")],
            metadata={"usage": {"total": 2}},
            extensions={"task": {"type": "review"}},
        ),
    ]
    for index, message in enumerate(original):
        message.generation_id = "generation-1"
        message.node_id = f"n{index}"
        message.parent_id = f"n{index - 1}" if index else "-"
        incoming = message.to_record() if index == 0 else message
        assert (
            await manager.query("append_message", {**payload, "message": incoming})
        )["success"]

    result = await manager.query(action, {**payload, "keyword": "find"})
    assert result["success"]
    assert len(result["messages"]["messages"]) == 2
    for found, message in zip(
        result["messages"]["messages"], reversed(original), strict=True
    ):
        assert type(found) is type(message)
        assert found.message_uid == message.message_uid
        assert found.generation_id == message.generation_id
        assert found.metadata == message.metadata
        assert found.extensions == message.extensions
        assert found.to_wire() == message.to_wire()
        assert all(
            not hasattr(found, key)
            for key in (
                "timestamp",
                "msg_cursor",
                "is_deleted",
                "title",
                "last_active_at",
            )
        )
    conversation = result["messages"]["conversations"][payload["conversation_uid"]]
    assert conversation["title"] == "New Conversation..."
    assert "T" in conversation["last_active_at"]
    empty = await manager.query(action, {**payload, "keyword": "missing"})
    assert empty == {
        "code": 0,
        "success": True,
        "messages": {"messages": [], "conversations": {}, "next_cursor": None},
    }
