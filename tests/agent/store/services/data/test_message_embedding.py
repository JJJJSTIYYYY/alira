"""Message vector maintenance, isolation and stable node search pagination."""

import asyncio
import struct
import sys
from types import SimpleNamespace

import pytest
from my_llm import ImageContent, ImageSource, ReasoningContent, TextContent
from sqlalchemy import delete, insert, select, text, update

from alira.database.services.data import DataService
from alira.database.services.data import service as service_module
from alira.database.services.data.embedding import MessageEmbedding
from alira.database.services.data.schema import messages
from alira.shared.models.message import (
    AliraAssistantMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from tests.agent.store.services.data.test_data_service import (
    create_user_and_conversation,
)

PAYLOAD = {"user_uid": "user-1", "conversation_uid": "conv-1"}


def vector(dimension=2, *, related=True):
    values = [1.0, 0.0] if related else [0.0, 1.0]
    return struct.pack(f"{dimension}f", *values, *([0.0] * (dimension - 2)))


@pytest.fixture
def embedding_settings(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    model_path = tmp_path / "model-a"
    model_path.mkdir()
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", True)
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_MODEL", str(model_path))
    monkeypatch.setattr(service_module, "VECTOR_DIMENSION", 2)
    encoded = []

    async def encode(self, texts, *, query=False):
        encoded.append((tuple(texts), query))
        return [
            vector(self._dimension, related="recipe" not in value) for value in texts
        ]

    monkeypatch.setattr(MessageEmbedding, "encode", encode)
    return encoded


@pytest.fixture
async def embedding_db(tmp_path, monkeypatch, embedding_settings):
    db = DataService(tmp_path / "vectors.sqlite3")
    await db.start()
    await create_user_and_conversation(db, monkeypatch)
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def search_db(tmp_path, monkeypatch, request):
    if request.node.callspec.params["method"] == "search_messages_vec":
        request.getfixturevalue("embedding_settings")
    else:
        monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", False)
    db = DataService(tmp_path / "search.sqlite3")
    await db.start()
    await create_user_and_conversation(db, monkeypatch)
    try:
        yield db
    finally:
        await db.stop()


async def append(db, uid, *, node=None, body="needle", payload=PAYLOAD):
    message = AliraUserMessage(
        message_uid=uid,
        node_id=uid if node is None else node,
        content=[TextContent(body)],
    )
    result = await db.append_message({**payload, "message": message.to_record()})
    assert result["success"], result


async def drain(db):
    if db._embedding_tasks:
        await asyncio.gather(*db._embedding_tasks, return_exceptions=True)


async def index_rows(db, table=None):
    return await db._fetch_all(
        select(table if table is not None else db._embedding.table)
    )


async def sync_completed(db):
    submitted = await db.sync_messages_embedding()
    assert submitted["success"], submitted
    await asyncio.wait_for(db._embedding_sync_task, timeout=3)
    result = await db.get_messages_embedding_sync_status(submitted["messages"])
    assert result["success"], result
    status = result["messages"]
    assert status["status"] == "completed", status
    assert status["error"] is None
    return {key: status[key] for key in ("inserted", "deleted")}


async def test_embedding_uses_only_user_and_assistant_text_bodies(
    embedding_db, embedding_settings
):
    db = embedding_db
    image = ImageContent(ImageSource("url", url="https://example.test/secret.png"))
    samples = [
        AliraUserMessage(
            message_uid="user", content=[TextContent("visible user"), image]
        ),
        AliraAssistantMessage(
            message_uid="assistant",
            content=[ReasoningContent("hidden thought"), TextContent("visible answer")],
            metadata={"secret": "metadata text"},
            extensions={"secret": "extension text"},
        ),
        AliraSystemMessage(content=[TextContent("hidden instruction")]),
        AliraSystemMessage(role="developer", content=[TextContent("hidden policy")]),
        AliraToolMessage(call_id="call-1", content=[TextContent("hidden tool")]),
        AliraAssistantMessage(content=[ReasoningContent("only reasoning")]),
        AliraUserMessage(content=[image]),
        AliraUserMessage(content=[TextContent("   ")]),
    ]
    for message in samples:
        result = await db.append_message({**PAYLOAD, "message": message.to_record()})
        assert result["success"], result
    await drain(db)
    documents = [
        value for batch, query in embedding_settings if not query for value in batch
    ]
    assert documents == ["visible user", "visible answer"]
    assert len(await index_rows(db)) == 2
    indexed = await db._fetch_all(
        select(messages.c.message_uid).where(
            messages.c.id.in_(select(db._embedding.table.c.message_id))
        )
    )
    assert {row["message_uid"] for row in indexed} == {"user", "assistant"}


async def test_append_returns_while_embedding_runs_and_stop_drains_it(
    embedding_db, monkeypatch
):
    db = embedding_db
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_encode(self, texts, *, query=False):
        entered.set()
        await release.wait()
        return [vector() for _ in texts]

    monkeypatch.setattr(MessageEmbedding, "encode", blocked_encode)
    try:
        await asyncio.wait_for(append(db, "nonblocking"), timeout=2)
        await asyncio.wait_for(entered.wait(), timeout=2)
        stopping = asyncio.create_task(db.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
    await asyncio.wait_for(stopping, timeout=2)
    assert db._engine is None
    await db.start()
    assert len(await index_rows(db)) == 1


async def test_background_failure_keeps_message_and_sync_repairs_index(
    embedding_db, monkeypatch
):
    db = embedding_db
    fake_encode = MessageEmbedding.encode

    async def failed_encode(self, texts, *, query=False):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(MessageEmbedding, "encode", failed_encode)
    await append(db, "retry")
    await drain(db)
    assert (await db.list_messages(PAYLOAD))["messages"][0]["message_uid"] == "retry"
    assert await index_rows(db) == []
    monkeypatch.setattr(MessageEmbedding, "encode", fake_encode)
    assert await sync_completed(db) == {"inserted": 1, "deleted": 0}
    assert len(await index_rows(db)) == 1


async def test_sync_submissions_share_one_background_task_and_stop_drains_it(
    embedding_db, monkeypatch
):
    db = embedding_db
    await append(db, "history")
    await drain(db)
    await db._execute(delete(db._embedding.table))
    assert not (await db.get_messages_embedding_sync_status())["success"]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def blocked_encode(self, texts, *, query=False):
        calls.append(texts)
        entered.set()
        await release.wait()
        return [vector() for _ in texts]

    monkeypatch.setattr(MessageEmbedding, "encode", blocked_encode)
    try:
        responses = await asyncio.wait_for(
            asyncio.gather(*(db.sync_messages_embedding({}) for _ in range(20))),
            timeout=2,
        )
        assert responses[0]["success"]
        assert all(response == responses[0] for response in responses)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert calls == [["needle"]]
        task_id = responses[0]["messages"]["task_id"]
        status = await db.get_messages_embedding_sync_status({"task_id": task_id})
        assert status["messages"] == {
            "task_id": task_id,
            "status": "running",
            "inserted": 0,
            "deleted": 0,
            "error": None,
        }
        assert await db.sync_messages_embedding() == responses[0]
        stopping = asyncio.create_task(db.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
    await asyncio.wait_for(stopping, timeout=2)
    completed = await db.get_messages_embedding_sync_status({"task_id": task_id})
    assert completed["messages"]["status"] == "completed"
    assert completed["messages"]["inserted"] == 1
    assert status["messages"]["status"] == "running"
    assert db._engine is None
    await db.start()
    assert len(await index_rows(db)) == 1
    assert not (await db.get_messages_embedding_sync_status())["success"]


async def test_sync_failure_is_queryable_and_allows_a_new_submission(
    embedding_db, monkeypatch
):
    db = embedding_db
    await append(db, "retry")
    await drain(db)
    await db._execute(delete(db._embedding.table))
    original_encode = MessageEmbedding.encode

    async def failed_encode(self, texts, *, query=False):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(MessageEmbedding, "encode", failed_encode)
    submitted = await db.sync_messages_embedding()
    assert submitted["success"]
    await asyncio.wait_for(db._embedding_sync_task, timeout=2)
    failed = await db.get_messages_embedding_sync_status(submitted["messages"])
    assert failed["messages"] == {
        "task_id": submitted["messages"]["task_id"],
        "status": "failed",
        "inserted": 0,
        "deleted": 0,
        "error": "RuntimeError: model unavailable",
    }
    monkeypatch.setattr(MessageEmbedding, "encode", original_encode)
    retry = await db.sync_messages_embedding()
    assert retry["success"] and retry != submitted
    assert not (await db.get_messages_embedding_sync_status(submitted["messages"]))[
        "success"
    ]
    await asyncio.wait_for(db._embedding_sync_task, timeout=2)
    completed = await db.get_messages_embedding_sync_status(retry["messages"])
    assert completed["messages"]["status"] == "completed"
    assert completed["messages"]["inserted"] == 1
    assert completed["messages"]["error"] is None


async def test_sync_removes_stale_rows_inserts_missing_and_skips_existing(
    embedding_db, embedding_settings
):
    db = embedding_db
    for uid in ("kept", "missing", "deleted"):
        await append(db, uid)
    await drain(db)
    table = db._embedding.table
    ids = {
        row["message_uid"]: row["id"]
        for row in await db._fetch_all(select(messages.c.id, messages.c.message_uid))
    }
    await db._execute(delete(table).where(table.c.message_id == ids["missing"]))
    # Simulate an interrupted cleanup and an orphan left by external SQL.
    await db._execute(
        update(messages).where(messages.c.id == ids["deleted"]).values(is_deleted=1)
    )
    await db._execute(insert(table).values(message_id=999999, embedding=vector()))
    await db._execute(
        update(table)
        .where(table.c.message_id == ids["kept"])
        .values(embedding=vector(related=False))
    )
    embedding_settings.clear()
    assert await sync_completed(db) == {"inserted": 1, "deleted": 2}
    assert embedding_settings == [(("needle",), False)]
    rows = {row["message_id"]: row["embedding"] for row in await index_rows(db)}
    assert rows == {ids["kept"]: vector(related=False), ids["missing"]: vector()}
    embedding_settings.clear()
    assert await sync_completed(db) == {
        "inserted": 0,
        "deleted": 0,
    }
    assert embedding_settings == []


async def test_sync_backfills_all_history_in_bounded_batches_after_enabling(
    embedding_db, embedding_settings, monkeypatch
):
    db = embedding_db
    await db.stop()
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", False)
    await db.start()
    for index in range(129):
        await append(db, f"history-{index}")
    skipped = [
        AliraUserMessage(
            content=[
                ImageContent(ImageSource("url", url="https://example.test/image.png"))
            ]
        ),
        AliraUserMessage(content=[]),
        AliraAssistantMessage(content=[ReasoningContent("only hidden thought")]),
        AliraToolMessage(call_id="history-tool", content=[TextContent("tool result")]),
    ]
    for message in skipped:
        result = await db.append_message({**PAYLOAD, "message": message.to_record()})
        assert result["success"], result
    await db.stop()
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", True)
    await db.start()
    assert await index_rows(db) == []
    assert embedding_settings == []
    original_encode = MessageEmbedding.encode
    second_batch = asyncio.Event()
    release = asyncio.Event()

    async def pause_second_batch(self, texts, *, query=False):
        if len(embedding_settings) == 1:
            second_batch.set()
            await release.wait()
        return await original_encode(self, texts, query=query)

    monkeypatch.setattr(MessageEmbedding, "encode", pause_second_batch)
    submitted = await db.sync_messages_embedding()
    try:
        await asyncio.wait_for(second_batch.wait(), timeout=2)
        progress = await db.get_messages_embedding_sync_status(submitted["messages"])
        assert progress["messages"]["status"] == "running"
        assert progress["messages"]["inserted"] == 64
    finally:
        release.set()
    await asyncio.wait_for(db._embedding_sync_task, timeout=2)
    result = await db.get_messages_embedding_sync_status(submitted["messages"])
    assert result["messages"]["status"] == "completed"
    assert result["messages"]["inserted"] == 129
    assert progress["messages"]["inserted"] == 64
    assert len(await index_rows(db)) == 129
    assert all(not query and len(batch) <= 64 for batch, query in embedding_settings)
    assert sum(len(batch) for batch, _ in embedding_settings) == 129
    embedding_settings.clear()
    assert await sync_completed(db) == {
        "inserted": 0,
        "deleted": 0,
    }
    assert embedding_settings == []


async def test_models_and_dimensions_are_isolated_and_delete_cleans_inactive_tables(
    embedding_db, tmp_path, monkeypatch
):
    db = embedding_db
    await append(db, "model-a-message", node="remove-a")
    await drain(db)
    table_a = db._embedding.table
    await db.stop()
    model_b = tmp_path / "model-b"
    model_b.mkdir()
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_MODEL", str(model_b))
    await db.start()
    table_b = db._embedding.table
    assert table_b.name != table_a.name
    assert await index_rows(db) == []
    await append(db, "model-b-message", node="remove-b")
    await drain(db)
    assert len(await index_rows(db)) == 1
    assert (await sync_completed(db))["inserted"] == 1
    assert len(await index_rows(db)) == 2
    await db.stop()
    monkeypatch.setattr(service_module, "VECTOR_DIMENSION", 3)
    await db.start()
    table_dimension = db._embedding.table
    assert table_dimension.name not in {table_a.name, table_b.name}
    assert await index_rows(db) == []
    await db.stop()
    # Cleanup remains necessary when embeddings are subsequently disabled.
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", False)
    await db.start()
    deleted = await db.delete_messages(
        {**PAYLOAD, "messages": ["remove-a", "remove-b"]}
    )
    assert deleted["success"], deleted
    assert await index_rows(db, table_a) == []
    assert await index_rows(db, table_b) == []
    assert await index_rows(db, table_dimension) == []


async def test_vector_search_filters_invalid_messages_before_page_limit(
    embedding_db, monkeypatch
):
    db = embedding_db
    for index in range(24):
        await append(db, f"visible-{index}")
    await append(db, "irrelevant", body="recipe")
    await append(db, "soft-deleted")
    await drain(db)
    await db._execute(
        update(messages)
        .where(messages.c.message_uid == "soft-deleted")
        .values(is_deleted=1)
    )
    await db._execute(
        insert(db._embedding.table).values(message_id=999999, embedding=vector())
    )
    await create_user_and_conversation(
        db, monkeypatch, user_uid="other", conversation_uid="other-conversation"
    )
    await append(
        db,
        "other-user",
        payload={"user_uid": "other", "conversation_uid": "other-conversation"},
    )
    monkeypatch.setattr(db, "_new_conversation_id", lambda: "deleted-conversation")
    assert (await db.create_conversation({"user_uid": "user-1"}))["success"]
    hidden_payload = {"user_uid": "user-1", "conversation_uid": "deleted-conversation"}
    await append(db, "hidden-conversation", payload=hidden_payload)
    assert (await db.update_conversation({**hidden_payload, "is_deleted": 1}))[
        "success"
    ]
    await drain(db)
    result = await db.search_messages_vec({"user_uid": "user-1", "keyword": "needle"})
    assert result["success"], result
    assert len(result["messages"]) == 20
    assert all(row["message_uid"].startswith("visible-") for row in result["messages"])
    assert all(row["similarity"] == pytest.approx(1.0) for row in result["messages"])
    second = await db.search_messages_vec(
        {"user_uid": "user-1", "keyword": "needle", "cursor": result["next_cursor"]}
    )
    assert len(second["messages"]) == 4
    broader = await db.search_messages_vec(
        {"user_uid": "user-1", "keyword": "needle", "min_similarity": 0.0}
    )
    assert "irrelevant" in {row["message_uid"] for row in broader["messages"]}
    invalid = await db.search_messages_vec(
        {"user_uid": "user-1", "keyword": "needle", "min_similarity": 1.1}
    )
    assert not invalid["success"]
    scoped = await db.search_messages_vec(
        {
            "user_uid": "user-1",
            "conversation_uid": "other-conversation",
            "keyword": "needle",
        }
    )
    assert scoped["messages"] == []


@pytest.mark.parametrize("method", ["search_messages", "search_messages_vec"])
async def test_search_pages_unique_nodes_by_timestamp_with_scoped_cursor_ties(
    search_db, monkeypatch, method
):
    db = search_db
    monkeypatch.setattr(db, "_new_conversation_id", lambda: "conv-2")
    assert (await db.create_conversation({"user_uid": "user-1"}))["success"]
    for conv in ("conv-1", "conv-2"):
        payload = {"user_uid": "user-1", "conversation_uid": conv}
        for index in range(25):
            await append(db, f"{conv}-{index}", node=f"node-{index}", payload=payload)
        await append(db, f"{conv}-duplicate", node="node-0", payload=payload)
    await drain(db)
    # Force timestamp ties across conversations and a first message newer than
    # its later append, so ordering must use timestamp before msg_cursor.
    await db._execute(update(messages).values(timestamp="2026-01-01 00:00:00"))
    await db._execute(
        update(messages)
        .where(messages.c.message_uid == "conv-1-0")
        .values(timestamp="2026-01-02 00:00:00")
    )
    representatives = {}
    for conv in ("conv-1", "conv-2"):
        rows = (
            await db.list_messages({"user_uid": "user-1", "conversation_uid": conv})
        )["messages"]
        for row in rows:
            key = (conv, row["node_id"])
            order = (row["timestamp"], row["msg_cursor"], conv)
            if key not in representatives or order > representatives[key][0]:
                representatives[key] = (order, row["message_uid"])
    expected = [uid for _, uid in sorted(representatives.values(), reverse=True)]
    found = []
    cursor = None
    sizes = []
    for _ in range(4):
        result = await getattr(db, method)(
            {"user_uid": "user-1", "keyword": "needle", "cursor": cursor}
        )
        assert result["success"], result
        sizes.append(len(result["messages"]))
        found.extend(row["message_uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert sizes[:3] == [20, 20, 10]
    assert found == expected
    assert len(set(found)) == 50


async def test_empty_node_ids_represent_independent_messages(embedding_db):
    db = embedding_db
    await append(db, "empty-node-1", node="")
    await append(db, "empty-node-2", node="")
    await drain(db)
    for method in (db.search_messages, db.search_messages_vec):
        result = await method({"user_uid": "user-1", "keyword": "needle"})
        assert {row["message_uid"] for row in result["messages"]} == {
            "empty-node-1",
            "empty-node-2",
        }


async def test_disabled_vector_search_delegates_to_text_search(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "SQLITE_EMBEDDING_ENABLE", False)
    db = DataService(tmp_path / "disabled.sqlite3")
    payload = {"user_uid": "user-1", "keyword": "needle", "cursor": {"msg_cursor": 4}}
    expected = {"success": True, "messages": [], "next_cursor": None}
    seen = []

    async def search(received):
        seen.append(received)
        return expected

    monkeypatch.setattr(db, "search_messages", search)
    await db.start()
    try:
        assert await db.search_messages_vec(payload) is expected
        assert seen == [payload]
        assert not (await db.sync_messages_embedding())["success"]
        tables = await db._fetch_all(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        assert not any(row["name"].startswith("messages_embedding_") for row in tables)
    finally:
        await db.stop()


async def test_local_encoder_loads_lazily_and_uses_query_instruction(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    calls = []

    class Model:
        def __init__(self, model_path, *, local_files_only):
            self.prompts = {"web_search_query": "retrieval instruction"}
            calls.append(("load", model_path, local_files_only))

        def get_embedding_dimension(self):
            return 2

        def encode_document(self, texts, **kwargs):
            calls.append(("document", texts, kwargs))
            return np.array([[1.0, 0.0] for _ in texts], dtype=np.float64)

        def encode_query(self, texts, **kwargs):
            calls.append(("query", texts, kwargs))
            return np.array([[1.0, 0.0] for _ in texts], dtype=np.float64)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    embedding = MessageEmbedding(str(tmp_path), 2)
    assert calls == []
    assert await embedding.encode([]) == []
    assert calls == []
    assert await embedding.encode(["body"]) == [vector()]
    assert await embedding.encode(["question"], query=True) == [vector()]
    assert calls[0] == ("load", str(tmp_path.resolve()), True)
    assert calls[1][0] == "document"
    assert calls[2][0] == "query"
    assert calls[2][2]["prompt_name"] == "web_search_query"
    assert sum(call[0] == "load" for call in calls) == 1
    await embedding.close()
    assert embedding._model is None


async def test_local_encoder_rejects_model_dimension_mismatch(tmp_path, monkeypatch):
    class Model:
        def __init__(self, model_path, **kwargs):
            pass

        def get_embedding_dimension(self):
            return 640

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    embedding = MessageEmbedding(str(tmp_path), 2)
    with pytest.raises(ValueError, match="dimension 640.*VECTOR_DIMENSION=2"):
        await embedding.encode(["body"])
    assert embedding._model is None
