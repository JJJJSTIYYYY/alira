import asyncio
import sqlite3
import threading
from contextlib import closing
from uuid import UUID

import pytest
from sqlalchemy import text

from alira.modules.database.services.data import DataService


@pytest.fixture
async def db(tmp_path):
    service = DataService(tmp_path / "nested" / "alira.sqlite3")
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


async def create_user_and_conversation(
    db: DataService, monkeypatch, *, user_uid="user-1", conversation_uid="conv-1"
):
    created = await db.create_user(
        {"user_uid": user_uid, "user_name": f"name-{user_uid}"}
    )
    assert created["success"] is True
    monkeypatch.setattr(db, "_new_conversation_id", lambda: conversation_uid)
    conversation = await db.create_conversation(
        {
            "user_uid": user_uid,
            "platform": "web",
            "title": "Initial title",
            "workspace": "/workspace/demo",
        }
    )
    assert conversation == {
        "code": 0,
        "success": True,
        "messages": {"conversation_uid": conversation_uid},
    }


@pytest.mark.asyncio
async def test_lifecycle_helpers_and_transaction_rollback(tmp_path):
    path = tmp_path / "deep" / "database.sqlite3"
    service = DataService(path)

    with pytest.raises(RuntimeError, match="call start"):
        await service._fetch_all(text("SELECT 1"))

    try:
        await asyncio.gather(service.start(), service.start())
        assert path.exists()
        columns = await service._fetch_all(text("PRAGMA table_info(users)"))
        assert {column["name"] for column in columns} == {
            "id",
            "user_uid",
            "user_name",
            "created_at",
        }
        tables = await service._fetch_all(
            text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        )
        assert {row["name"] for row in tables} >= {
            "users",
            "conversations",
            "messages",
            "compressed_context",
            "llm_provider",
            "mcp_server",
            "cron_task",
        }
        assert {"longterm_memory", "agent_skills"}.isdisjoint(
            row["name"] for row in tables
        )
        message_columns = [
            row["name"]
            for row in await service._fetch_all(text("PRAGMA table_info(messages)"))
        ]
        assert message_columns == [
            "id",
            "message_uid",
            "msg_cursor",
            "user_uid",
            "conversation_id",
            "conversation_uid",
            "generation_id",
            "node_id",
            "parent_id",
            "role",
            "name",
            "content",
            "protocol",
            "item",
            "origin",
            "call_id",
            "metadata",
            "extensions",
            "timestamp",
            "is_deleted",
        ]

        with pytest.raises(RuntimeError, match="force rollback"):
            async with service._transaction() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users (user_uid, user_name) "
                        "VALUES (:user_uid, :user_name)"
                    ),
                    {
                        "user_uid": "rolled-back",
                        "user_name": "rolled-back",
                    },
                )
                raise RuntimeError("force rollback")
        assert (
            await service._fetch_all(
                text("SELECT * FROM users WHERE user_uid = :user_uid"),
                {"user_uid": "rolled-back"},
            )
            == []
        )

    finally:
        await service.stop()
    await service.stop()
    with pytest.raises(RuntimeError, match="call start"):
        await service._execute(text("SELECT 1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("db_name", ["store #1?.sqlite3", ":memory:"])
async def test_repeated_start_and_stop_preserve_expected_storage(tmp_path, db_name):
    path = db_name if db_name == ":memory:" else tmp_path / db_name
    service = DataService(path)
    user = {"user_uid": "user-1", "user_name": "alice"}
    try:
        await asyncio.gather(service.start(), service.start())
        assert (await service.create_user(user))["success"]
        await service.start()
        assert (await service.verify_user(user))["success"]

        await asyncio.gather(service.stop(), service.stop())
        await service.start()
        restored = await service.verify_user(user)
        if db_name == ":memory:":
            assert restored["success"] is True
            assert restored["code"] == 2001
            assert (await service.create_user(user))["success"]
        else:
            assert restored["messages"]["user_uid"] == "user-1"

        assert (await service.create_conversation({"user_uid": "missing"}))[
            "success"
        ] is False
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wal_enabled", [False, True])
async def test_multiple_services_can_initialize_the_same_database(
    tmp_path, wal_enabled
):
    path = tmp_path / "shared.sqlite3"
    if wal_enabled:
        with closing(sqlite3.connect(path)) as connection:
            assert (
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            )
    services = [DataService(path) for _ in range(3)]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(service.start() for service in services), return_exceptions=True
            ),
            timeout=10,
        )
        assert results == [None, None, None], results
        for index, service in enumerate(services):
            assert (
                await service.create_user(
                    {
                        "user_uid": f"user-{index}",
                        "user_name": f"user-{index}",
                    }
                )
            )["success"]
        assert await services[0]._fetch_all(
            text("SELECT user_uid FROM users ORDER BY user_uid")
        ) == [{"user_uid": f"user-{index}"} for index in range(3)]
    finally:
        await asyncio.gather(*(service.stop() for service in services))


@pytest.mark.asyncio
async def test_cancelled_transaction_rolls_back_and_allows_stop_and_restart(tmp_path):
    service = DataService(tmp_path / "cancelled.sqlite3")
    entered = asyncio.Event()
    release = asyncio.Event()
    task = None

    async def pending_write():
        async with service._transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (user_uid, user_name) "
                    "VALUES (:user_uid, :user_name)"
                ),
                {
                    "user_uid": "cancelled",
                    "user_name": "cancelled",
                },
            )
            entered.set()
            await release.wait()

    try:
        await service.start()
        task = asyncio.create_task(pending_write())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        await asyncio.wait_for(service.stop(), timeout=2)
        await asyncio.wait_for(service.start(), timeout=2)
        assert (
            await service._fetch_all(
                text("SELECT user_uid FROM users WHERE user_uid = :user_uid"),
                {"user_uid": "cancelled"},
            )
            == []
        )
        assert (
            await service.create_user(
                {"user_uid": "cancelled", "user_name": "cancelled"}
            )
        )["success"]
        assert (await service.verify_user({"user_name": "cancelled"}))["messages"][
            "user_uid"
        ] == "cancelled"
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelling_active_sql_keeps_the_in_memory_database(cancel_count):
    service = DataService(":memory:")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    task = None
    user = {"user_uid": "existing", "user_name": "existing"}

    def slow_sql():
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=5):
            raise RuntimeError("SQL query was not released")
        return "cancelled"

    try:
        await service.start()
        assert (await service.create_user(user))["success"]
        async with service._transaction() as connection:
            await connection.run_sync(
                lambda sync_connection: sync_connection.connection.create_function(
                    "slow_sql", 0, slow_sql
                )
            )
        task = asyncio.create_task(
            service._execute(
                text(
                    "INSERT INTO users (user_uid, user_name) "
                    "VALUES ('cancelled', slow_sql())"
                )
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert (await service.verify_user(user))["messages"]["user_uid"] == "existing"
        assert (await service.create_user({"user_uid": "new", "user_name": "new"}))[
            "success"
        ]
        assert await service._fetch_all(
            text("SELECT user_uid FROM users ORDER BY user_uid")
        ) == [{"user_uid": "existing"}, {"user_uid": "new"}]
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("service_count", [1, 2])
async def test_concurrent_appends_allocate_unique_contiguous_cursors(
    tmp_path, monkeypatch, service_count
):
    path = tmp_path / "concurrent.sqlite3"
    services = [DataService(path) for _ in range(service_count)]
    payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
    count = 24
    try:
        for service in services:
            await service.start()
        await create_user_and_conversation(services[0], monkeypatch)
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    services[index % service_count].append_message(
                        {
                            **payload,
                            "message": {
                                "message_uid": f"message-{index}",
                                "node_id": f"node-{index}",
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": f"concurrent-{index}"}
                                ],
                            },
                        }
                    )
                    for index in range(count)
                )
            ),
            timeout=10,
        )
        assert all(result["success"] for result in results), results
        assert sorted(result["messages"]["msg_cursor"] for result in results) == list(
            range(1, count + 1)
        )
        fetched = await services[0].list_messages(payload)
        assert [row["msg_cursor"] for row in fetched["messages"]] == list(
            range(1, count + 1)
        )
        assert len({row["message_uid"] for row in fetched["messages"]}) == count
        meta = await services[0].get_conversation(payload)
        assert meta["messages"][0]["latest_cursor"] == count
    finally:
        await asyncio.gather(*(service.stop() for service in services))


@pytest.mark.asyncio
async def test_failed_message_insert_does_not_advance_conversation_cursor(
    db, monkeypatch
):
    await create_user_and_conversation(db, monkeypatch)
    payload = {
        "user_uid": "user-1",
        "conversation_uid": "conv-1",
        "message": {
            "message_uid": "message-1",
            "role": "user",
            "content": [{"type": "text", "text": "first"}],
        },
    }
    assert (await db.append_message(payload))["messages"]["msg_cursor"] == 1
    assert (await db.append_message(payload))["success"] is False
    meta = await db.get_conversation(payload)
    assert meta["messages"][0]["latest_cursor"] == 1
    assert meta["messages"][0]["has_new_message"] == 0

    payload["message"] = {
        "message_uid": "message-2",
        "role": "assistant",
        "content": [{"type": "text", "text": "second"}],
    }
    assert (await db.append_message(payload))["messages"]["msg_cursor"] == 2
    fetched = await db.list_messages(payload)
    assert [row["message_uid"] for row in fetched["messages"]] == [
        "message-1",
        "message-2",
    ]


@pytest.mark.asyncio
async def test_users_conversations_and_messages(db, monkeypatch):
    assert await db.verify_user({"user_uid": "user-1"}) == {
        "success": True,
        "code": 2001,
        "messages": "User not found.",
    }
    identity = {"user_uid": "user-1", "user_name": "alice"}
    assert (await db.create_user(identity))["success"]
    for query in ({"user_uid": "user-1"}, {"user_name": "alice"}, identity):
        assert await db.verify_user(query) == {
            "success": True,
            "code": 0,
            "messages": identity,
        }
    assert (await db.verify_user({"user_uid": "new", "user_name": "alice"}))[
        "code"
    ] == 2001
    assert (await db.create_user({"user_uid": "duplicate", "user_name": "alice"}))[
        "code"
    ] == 2002

    monkeypatch.setattr(db, "_new_conversation_id", lambda: "conv-1")
    assert await db.create_conversation(
        {
            "user_uid": "user-1",
            "title": None,
            "workspace": "/tmp/work",
            "is_cron": True,
        }
    ) == {"code": 0, "success": True, "messages": {"conversation_uid": "conv-1"}}
    conversations = (await db.list_conversations({"user_uid": "user-1"}))["messages"]
    assert conversations[0]["title"] == "New Conversation..."
    assert conversations[0]["work_space"] == "/tmp/work"
    assert conversations[0]["is_cron"] == 1
    assert "T" in conversations[0]["created_at"]

    assert await db.update_conversation(
        {
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "title": "Renamed",
            "workspace": "/tmp/new",
            "is_pinned": True,
        }
    ) == {"code": 0, "success": True, "messages": {}}
    meta = (await db.get_conversation({"conversation_uid": "conv-1"}))["messages"][0]
    assert (meta["title"], meta["work_space"], meta["is_pinned"]) == (
        "Renamed",
        "/tmp/new",
        1,
    )

    first = await db.append_message(
        {
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "message": {
                "message_uid": "message-1",
                "role": "user",
                "name": "alice",
                "content": [{"type": "text", "text": "hello sqlite"}],
                "node_id": "node-1",
                "metadata": None,
                "extensions": {"uploaded_files": ["brief.pdf"]},
            },
        }
    )
    second = await db.append_message(
        {
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "message": {
                "message_uid": "message-2",
                "role": "assistant",
                "name": "assistant",
                "content": [{"type": "text", "text": "hello back"}],
                "generation_id": "gen-1",
                "node_id": "node-2",
                "parent_id": "node-1",
                "metadata": {"model": "demo-model"},
                "extensions": {"reasoning": "because"},
            },
        }
    )
    assert first["messages"]["msg_cursor"] == 1
    assert second["messages"]["msg_cursor"] == 2
    assert "T" in first["messages"]["timestamp"]
    conversation_row = (
        await db._fetch_all(
            text("SELECT latest_cursor, has_new_message FROM conversations")
        )
    )[0]
    assert conversation_row == {
        "latest_cursor": 2,
        "has_new_message": 1,
    }

    fetched = await db.list_messages(
        {
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "cursor": -5,
            "limit": 1,
        }
    )
    assert [row["msg_cursor"] for row in fetched["messages"]] == [1]
    assert fetched["next_cursor"] == 2
    assert fetched["messages"][0]["message_uid"] == "message-1"
    assert fetched["messages"][0]["name"] == "alice"
    assert fetched["messages"][0]["metadata"] == {}
    assert fetched["messages"][0]["extensions"] == {"uploaded_files": ["brief.pdf"]}
    assert (
        await db.list_messages(
            {"user_uid": "user-1", "conversation_uid": "conv-1", "cursor": 99}
        )
    )["next_cursor"] == 99

    matches = await db.search_messages(
        {"user_uid": "user-1", "keyword": "hello_sqlite"}
    )
    assert [row["content"][0]["text"] for row in matches["messages"]] == [
        "hello sqlite"
    ]
    assert await db.search_messages({"user_uid": "user-1", "keyword": "% _ \\"}) == {
        "code": 0,
        "success": True,
        "messages": [],
        "next_cursor": None,
    }

    deleted = await db.delete_messages(
        {
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "messages": ["node-1", "missing"],
        }
    )
    assert deleted == {
        "code": 0,
        "success": True,
        "messages": [{"message_uid": "message-1"}],
    }
    assert (await db.search_messages({"user_uid": "user-1", "keyword": "sqlite"}))[
        "messages"
    ] == []

    for result in (
        await db.append_message(
            {"user_uid": "user-1", "conversation_uid": "conv-1", "message": {}}
        ),
        await db.delete_messages(
            {"user_uid": "user-1", "conversation_uid": "conv-1", "messages": []}
        ),
    ):
        assert result == {
            "success": True,
            "code": 1001,
            "messages": "Invalid request payload.",
        }


@pytest.mark.asyncio
async def test_compressed_context(db, monkeypatch):
    await create_user_and_conversation(db, monkeypatch)

    assert await db.create_compressed_context(
        {
            "memory_id": "memory-1",
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "content": "remember this",
        }
    ) == {"code": 0, "success": True, "messages": {}}
    memory = (
        await db.list_compressed_contexts(
            {"user_uid": "user-1", "conversation_uid": "conv-1"}
        )
    )["messages"][0]
    assert memory["memory_id"] == "memory-1"
    await db.delete_compressed_contexts(
        {
            "memory_ids": ["memory-1"],
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
        }
    )
    assert (
        await db.list_compressed_contexts(
            {"user_uid": "user-1", "conversation_uid": "conv-1"}
        )
    )["messages"] == []
    assert (
        await db.delete_compressed_contexts(
            {"memory_ids": [], "user_uid": "user-1", "conversation_uid": "conv-1"}
        )
    )["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "create_method,read_method,payload,json_field",
    [
        (
            "create_llm_provider",
            "list_llm_providers",
            {
                "provider_id": "provider-1",
                "provider_name": "Primary",
                "endpoint": "https://example.test/v1",
                "model_list": ["null", "[]"],
            },
            "model_list",
        ),
        (
            "create_mcp_server",
            "list_mcp_servers",
            {
                "mcp_id": "mcp-1",
                "mcp_name": "Tools",
                "transport": "stdio",
                "endpoint": None,
                "config": {"command": "demo", "env": {"DEFAULT": "null"}},
            },
            "config",
        ),
        (
            "create_cron_task",
            "list_cron_tasks",
            {
                "task_id": "task-1",
                "conversation_uid": "conv-1",
                "task_name": "Daily check",
                "exec_time": "2026-07-22 09:30:00",
                "extra_config": {"weekdays": [1, 3], "label": "[]"},
            },
            "extra_config",
        ),
    ],
)
async def test_json_reads_survive_restart_and_do_not_share_state(
    db, monkeypatch, create_method, read_method, payload, json_field
):
    await create_user_and_conversation(db, monkeypatch)
    created = await getattr(db, create_method)({"user_uid": "user-1", **payload})
    assert created["success"]

    result = await getattr(db, read_method)({"user_uid": "user-1"})
    assert result["success"]
    value = result["messages"][0][json_field]
    assert value == payload[json_field]
    value.clear()

    await db.stop()
    await db.start()
    restored = await getattr(db, read_method)({"user_uid": "user-1"})
    assert restored["success"]
    assert restored["messages"][0][json_field] == payload[json_field]


@pytest.mark.asyncio
async def test_provider_mcp_and_cron_crud(db, monkeypatch):
    await create_user_and_conversation(db, monkeypatch)

    assert await db.create_llm_provider(
        {
            "provider_id": "provider-1",
            "user_uid": "user-1",
            "provider_name": "Primary",
            "type": "OPENAI",
            "endpoint": "https://example.test/v1",
            "model_list": ["model-a"],
            "description": "initial",
        }
    ) == {"code": 0, "success": True, "messages": {"provider_id": "provider-1"}}
    provider = (await db.get_llm_provider({"provider_id": "provider-1"}))["messages"][0]
    assert provider["type"] == "openai"
    assert provider["model_list"] == ["model-a"]
    assert (await db.list_llm_providers({"user_uid": "user-1"}))["messages"] == [
        provider
    ]
    assert await db.update_llm_provider(
        {
            "provider_id": "provider-1",
            "user_uid": "user-1",
            "provider_name": "Updated",
            "type": "ANTHROPIC",
            "model_list": ["claude"],
        }
    ) == {"code": 0, "success": True, "messages": {}}
    provider = (await db.get_llm_provider({"provider_id": "provider-1"}))["messages"][0]
    assert provider["provider_name"] == "Updated"
    assert provider["type"] == "anthropic"
    assert provider["model_list"] == ["claude"]
    assert (
        await db.create_llm_provider(
            {
                "provider_id": "provider-1",
                "user_uid": "user-1",
                "provider_name": "duplicate",
                "endpoint": "x",
                "model_list": [],
            }
        )
    )["success"] is False

    assert await db.create_mcp_server(
        {
            "mcp_id": "mcp-1",
            "user_uid": "user-1",
            "mcp_name": "Tools",
            "transport": "stdio",
            "endpoint": None,
            "config": {"command": "demo"},
            "description": "tool server",
        }
    ) == {"code": 0, "success": True, "messages": {"mcp_id": "mcp-1"}}
    mcp = (await db.list_mcp_servers({"user_uid": "user-1"}))["messages"][0]
    assert mcp["config"] == {"command": "demo"}
    enabled_mcp = (await db.list_enabled_mcp_servers({"user_uid": "user-1"}))[
        "messages"
    ]
    assert len(enabled_mcp) == 1
    assert enabled_mcp[0]["config"] == {"command": "demo"}
    await db.update_mcp_server(
        {
            "mcp_id": "mcp-1",
            "user_uid": "user-1",
            "mcp_name": "Updated tools",
            "config": {"command": "new"},
            "enabled": False,
            "tool_count": 7,
        }
    )
    assert (await db.list_enabled_mcp_servers({"user_uid": "user-1"}))["messages"] == []
    mcp = (await db.list_mcp_servers({"user_uid": "user-1"}))["messages"][0]
    assert (mcp["mcp_name"], mcp["tool_count"]) == ("Updated tools", 7)
    assert mcp["config"] == {"command": "new"}

    assert await db.create_cron_task(
        {
            "task_id": "task-1",
            "user_uid": "user-1",
            "conversation_uid": "conv-1",
            "task_name": "Daily check",
            "prompt": "check",
            "execute": "run()",
            "exec_time": "2026-07-22 09:30:00",
            "repeat": "day",
            "extra_config": {"timezone": "Asia/Tokyo"},
        }
    ) == {"code": 0, "success": True, "messages": {"task_id": "task-1"}}
    task = (await db.get_cron_task({"task_id": "task-1"}))["messages"][0]
    assert task["platform"] == "default"
    assert task["exec_time"] == "2026-07-22T09:30:00"
    assert task["extra_config"] == {"timezone": "Asia/Tokyo"}
    tasks = (await db.list_cron_tasks({"user_uid": "user-1"}))["messages"]
    enabled_tasks = (await db.list_enabled_cron_tasks({}))["messages"]
    assert len(tasks) == len(enabled_tasks) == 1
    assert (
        tasks[0]["extra_config"]
        == enabled_tasks[0]["extra_config"]
        == task["extra_config"]
    )
    assert await db.update_cron_task(
        {
            "task_id": "task-1",
            "task_name": "Weekly check",
            "repeat": "week",
            "extra_config": {"weekday": 1},
            "enabled": False,
        }
    ) == {"code": 0, "success": True, "messages": {}}
    task = (await db.get_cron_task({"task_id": "task-1"}))["messages"][0]
    assert (task["name"], task["repeat"], task["enabled"]) == (
        "Weekly check",
        "week",
        0,
    )
    assert task["extra_config"] == {"weekday": 1}
    assert (await db.list_enabled_cron_tasks({}))["messages"] == []
    await db.update_cron_task({"task_id": "task-1", "is_deleted": True})
    assert (await db.get_cron_task({"task_id": "task-1"}))["messages"] == []

    await db.update_llm_provider(
        {"provider_id": "provider-1", "user_uid": "user-1", "is_deleted": True}
    )
    await db.update_mcp_server(
        {"mcp_id": "mcp-1", "user_uid": "user-1", "is_deleted": True}
    )
    assert (await db.list_llm_providers({"user_uid": "user-1"}))["messages"] == []
    assert (await db.list_mcp_servers({"user_uid": "user-1"}))["messages"] == []


@pytest.mark.parametrize("resource", ["provider", "mcp", "cron"])
@pytest.mark.parametrize(
    "value", ["null", '{"literal":[1,2]}', {"nested": ["null", "[]"]}]
)
async def test_json_values_keep_their_type_across_create_and_update(
    db, monkeypatch, resource, value
):
    await create_user_and_conversation(db, monkeypatch)
    operations = {
        "provider": (
            db.create_llm_provider,
            db.update_llm_provider,
            db.get_llm_provider,
            "model_list",
            {
                "provider_id": "json-provider",
                "user_uid": "user-1",
                "provider_name": "JSON",
                "endpoint": "https://example.test",
            },
        ),
        "mcp": (
            db.create_mcp_server,
            db.update_mcp_server,
            db.list_mcp_servers,
            "config",
            {
                "mcp_id": "json-mcp",
                "user_uid": "user-1",
                "mcp_name": "JSON",
                "transport": "stdio",
                "endpoint": None,
            },
        ),
        "cron": (
            db.create_cron_task,
            db.update_cron_task,
            db.get_cron_task,
            "extra_config",
            {
                "task_id": "json-cron",
                "user_uid": "user-1",
                "conversation_uid": "conv-1",
                "task_name": "JSON",
                "exec_time": "2026-01-01T00:00:00",
            },
        ),
    }
    create, update, read, field, payload = operations[resource]
    assert (await create({**payload, field: value}))["success"]
    assert (await read(payload))["messages"][0][field] == value
    assert (await update({**payload, field: value}))["success"]
    assert (await read(payload))["messages"][0][field] == value
    assert (await update({**payload, field: None}))["success"]
    assert (await read(payload))["messages"][0][field] == value


@pytest.mark.parametrize("field", ["metadata", "extensions"])
async def test_message_objects_reject_encoded_text_before_allocating_a_cursor(
    db, monkeypatch, field
):
    await create_user_and_conversation(db, monkeypatch)
    payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
    result = await db.append_message(
        {
            **payload,
            "message": {
                "message_uid": "invalid-object",
                "role": "user",
                field: '{"encoded":true}',
            },
        }
    )
    assert result["success"] is True
    assert result["code"] == 1001
    assert result["messages"] == "Invalid request payload."
    assert (await db.list_messages(payload))["messages"] == []
    conversation = (await db.get_conversation(payload))["messages"][0]
    assert conversation["latest_cursor"] == 0


def test_data_service_generates_uuid4_strings(tmp_path):
    service = DataService(tmp_path / "data.sqlite3")

    identifiers = [service._new_conversation_id() for _ in range(100)]

    assert len(set(identifiers)) == len(identifiers)
    for identifier in identifiers:
        assert isinstance(identifier, str)
        parsed = UUID(identifier)
        assert parsed.version == 4
        assert str(parsed) == identifier


@pytest.mark.parametrize(
    ("method", "payload", "code"),
    [
        ("verify_user", {"user_uid": "missing"}, 2001),
        ("verify_user", {}, 1001),
        ("get_messages_embedding_sync_status", {}, 2005),
        ("sync_messages_embedding", {}, 2004),
        ("delete_messages", {"messages": []}, 1001),
        (
            "append_message",
            {"user_uid": "missing", "conversation_uid": "missing", "message": {}},
            1001,
        ),
        (
            "append_message",
            {
                "user_uid": "missing",
                "conversation_uid": "missing",
                "message": {
                    "message_uid": "m",
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
            },
            2003,
        ),
    ],
)
async def test_business_errors_return_directly_without_the_exception_handler(
    db, method, payload, code
):
    from alira.modules.database.utils.errors import ERROR_MESSAGES, StoreCode

    operation = getattr(type(db), method).__wrapped__
    assert await operation(db, payload) == {
        "success": True,
        "code": code,
        "messages": ERROR_MESSAGES[StoreCode(code)],
    }
    assert await db._fetch_all(text("SELECT user_uid FROM users")) == []
    assert await db._fetch_all(text("SELECT message_uid FROM messages")) == []
