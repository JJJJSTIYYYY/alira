"""Check query plans from the SQL actually executed by DataService."""

from my_llm import TextContent
from sqlalchemy import event, text

from alira.agent.core.utils.message import AliraToolMessage, AliraUserMessage
from alira.database.services.data import DataService
from tests.agent.store.services.data.test_data_service import (
    create_user_and_conversation,
)


async def test_message_queries_use_scoped_indexes(tmp_path, monkeypatch):
    db = DataService(tmp_path / "plans.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        for index in range(30):
            message = AliraUserMessage(
                content=[TextContent(f"needle-{index}")], node_id=f"n{index}"
            )
            assert (await db.append_message({**payload, "message": message.to_dict()}))[
                "success"
            ]
        statements = []

        def capture(_conn, _cursor, statement, parameters, _context, _many):
            if statement.startswith(("SELECT", "UPDATE messages")):
                statements.append((statement, parameters))

        event.listen(db._engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await db.list_messages({**payload, "cursor": 15}))["success"]
            assert (await db.delete_messages({**payload, "messages": ["n1"]}))[
                "success"
            ]
            assert (
                await db.search_messages({"user_uid": "user-1", "keyword": "needle"})
            )["success"]
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", capture)
        plans = []
        async with db._transaction() as connection:
            for statement, parameters in statements:
                result = await connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                )
                plans.append(" ".join(row[3] for row in result))
        assert len(plans) == 3
        assert "idx_messages_user_conversation_cursor" in plans[0]
        assert "TEMP B-TREE" not in plans[0]
        assert "idx_messages_user_conversation_node" in plans[1]
        assert "idx_messages_conversation_search" in plans[2]
        assert "SCAN messages" not in " ".join(plans)
        # Both identities also have unique lookup indexes supplied by constraints.
        for column in ("message_uid",):
            rows = await db._fetch_all(
                text(f"EXPLAIN QUERY PLAN SELECT id FROM messages WHERE {column} = 'x'")
            )
            assert "USING COVERING INDEX sqlite_autoindex_messages" in rows[0]["detail"]
    finally:
        await db.stop()


async def test_tool_results_do_not_reset_notifications_or_enter_user_search(
    tmp_path, monkeypatch
):
    db = DataService(tmp_path / "tool-results.sqlite3")
    await db.start()
    try:
        await create_user_and_conversation(db, monkeypatch)
        payload = {"user_uid": "user-1", "conversation_uid": "conv-1"}
        message = AliraToolMessage(
            call_id="c1", content=[TextContent("tool-only-needle")]
        )
        assert (await db.append_message({**payload, "message": message.to_dict()}))[
            "success"
        ]
        meta = (await db.get_conversation(payload))["messages"][0]
        assert meta["has_new_message"] == 1
        matches = await db.search_messages(
            {"user_uid": "user-1", "keyword": "tool-only-needle"}
        )
        assert matches == {"success": True, "messages": []}
        rows = (await db.list_messages(payload))["messages"]
        assert rows[0]["role"] == "tool" and rows[0]["call_id"] == "c1"
        user = AliraUserMessage(content=[TextContent("user-only-needle")])
        assert (await db.append_message({**payload, "message": user.to_dict()}))[
            "success"
        ]
        assert (await db.get_conversation(payload))["messages"][0][
            "has_new_message"
        ] == 0
        matches = await db.search_messages(
            {"user_uid": "user-1", "keyword": "user-only-needle"}
        )
        assert [row["role"] for row in matches["messages"]] == ["user"]
    finally:
        await db.stop()
