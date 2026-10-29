import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from uuid import uuid4

from apix.agent.store.core.service.utils.decorator import service_handler
from apix.agent.store.core.service.utils.message_fields import (
    decode_message_row,
    encode_content,
    encode_optional,
)
from apix.common.lifespan.auto_init import auto_init
from apix.config.base_config import SQLITE_DATABASE


T = TypeVar("T")


def _identity_failure(exc: Exception) -> dict:
    return {
        "success": False,
        "messages": {
            "msg": f"{type(exc).__name__}: {exc}",
            "uid": None,
        },
    }


class DataService:
    """SQLite persistence with one connection and serialized worker-thread calls.

    Call ``start()`` before storage operations and ``stop()`` to close the
    connection. Storage methods return result dictionaries rather than raising
    ordinary exceptions; lifecycle methods propagate their exceptions.

    Row dictionaries contain the selected SQLite columns. Flags are integers,
    and SQLite date-time strings are normalized to use a ``T`` separator.
    JSON columns remain strings unless a method explicitly documents decoding.
    Constraints and foreign keys are defined in ``utils/init_sqlite.sql``;
    payloads are not otherwise schema-validated by this service.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        """Create a service for ``db_path`` without opening the database.

        Args:
            db_path: SQLite file path, or ``":memory:"``. ``None`` uses
                ``SQLITE_DATABASE``. File paths expand ``~``.
        """
        default_path = SQLITE_DATABASE
        selected_path = str(db_path if db_path is not None else default_path)
        self._db_path = (
            selected_path
            if selected_path == ":memory:"
            else str(Path(selected_path).expanduser())
        )
        self._connection: Optional[sqlite3.Connection] = None
        self._connection_lock = asyncio.Lock()
        self._schema_path = Path(__file__).with_name("utils") / "init_sqlite.sql"

    def _conversation_id_generator(self) -> str:
        """Generate a unique conversation ID using UUID4."""
        return str(uuid4())

    async def start(self):
        """Open the database and create missing tables and indexes.

        Repeated calls while open do nothing. Creates the database's parent
        folder when needed. Returns ``None``; initialization errors propagate.
        """
        async with self._connection_lock:
            if self._connection is not None:
                return

            if self._db_path != ":memory:":
                Path(self._db_path).expanduser().resolve().parent.mkdir(
                    parents=True, exist_ok=True
                )
            schema = self._schema_path.read_text(encoding="utf-8")

            def open_database() -> sqlite3.Connection:
                connection = sqlite3.connect(
                    self._db_path,
                    timeout=30,
                    check_same_thread=False,
                )
                try:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute("PRAGMA busy_timeout = 30000")
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(schema)
                    connection.commit()
                    return connection
                except Exception:
                    connection.close()
                    raise

            self._connection = await asyncio.to_thread(open_database)

    async def stop(self):
        """Commit pending work and close the database connection.

        Does nothing when already closed. Returns ``None``; closing errors
        propagate. The service can be opened again with ``start()``.
        """
        async with self._connection_lock:
            connection = self._connection
            if connection is None:
                return
            self._connection = None

            def close_database() -> None:
                connection.commit()
                connection.close()

            await asyncio.to_thread(close_database)

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._connection_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError(
                    "SQLite database is not initialized, call start() first"
                )

            def run_operation() -> T:
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise

            return await asyncio.to_thread(run_operation)

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            return connection.execute(sql, params).rowcount

        return await self._run(operation)

    async def _executemany(
        self, sql: str, params: list[tuple[Any, ...]]
    ) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            return connection.executemany(sql, params).rowcount

        return await self._run(operation)

    async def _fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            return [
                self._normalize_row(dict(row))
                for row in connection.execute(sql, params).fetchall()
            ]

        return await self._run(operation)

    @staticmethod
    def _json(value: Any, default: Any) -> str:
        if value is None:
            value = default
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        """Normalize SQLite timestamps to ISO format."""
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if key.endswith("_at") or key in {"exec_time", "timestamp"}:
                if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
                    row[key] = value.replace(" ", "T", 1)
        return row

    # --------------------------------------------------
    # Users
    # --------------------------------------------------

    @service_handler(failure_factory=_identity_failure)
    async def create_a_user(self, payload: dict) -> dict:
        """Insert a user account; store the supplied password unchanged.

        Args:
            payload: Required ``user_uid``, ``username`` and ``password`` strings.
                User IDs and usernames must each be unique.

        Returns:
            Success: ``{"success": True, "messages": {"msg": "success", "uid": user_uid}}``.
            Failure: ``{"success": False, "messages": {"msg": "<ExceptionType>: <error>", "uid": None}}``.
        """
        user_uid = payload["user_uid"]
        await self._execute(
            "INSERT INTO users (user_uid, username, password) VALUES (?, ?, ?)",
            (user_uid, payload["username"], payload["password"]),
        )
        return {
            "success": True,
            "messages": {"msg": "success", "uid": user_uid},
        }

    @service_handler(failure_factory=_identity_failure)
    async def verify_user(self, payload: dict) -> dict:
        """Match a username and password against a stored account.

        Args:
            payload: Required ``username`` and ``password`` strings, compared
                directly with the stored values.

        Returns:
            Success: ``{"success": True, "messages": {"msg": "success", "uid": user_uid}}``.
            Failure: ``{"success": False, "messages": {"msg": "<ExceptionType>: <error>", "uid": None}}``.
                Zero matches are a failure, as are database errors.
        """
        rows = await self._fetch_all(
            "SELECT user_uid, username FROM users "
            "WHERE username = ? AND password = ?",
            (payload["username"], payload["password"]),
        )
        if len(rows) != 1:
            raise Exception("User do not exist or wrong password.")
        return {
            "success": True,
            "messages": {"msg": "success", "uid": rows[0]["user_uid"]},
        }

    @service_handler
    async def ensure_user_exists(self, payload: dict, exist: bool = True) -> dict:
        """Check whether a user ID or username exists.

        Args:
            payload: Required ``user_uid``; optional ``username`` (default ``None``).
                A row matching either value counts as an existing user.
            exist: ``True`` (default) requires a match; ``False`` requires none.

        Returns:
            Success: ``{"success": True, "messages": "success"}``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``, including
                when the existence check does not meet ``exist``.
        """
        rows = await self._fetch_all(
            "SELECT user_uid, username FROM users "
            "WHERE user_uid = ? OR username = ?",
            (payload["user_uid"], payload.get("username")),
        )
        if exist and not rows:
            raise Exception("User do not exist.")
        if not exist and rows:
            raise Exception("User has already exist.")
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Conversations
    # --------------------------------------------------

    @service_handler
    async def fetch_conversation_list(self, payload: dict) -> dict:
        """List a user's conversations, pinned first and then most recently active.

        Args:
            payload: Required ``user_uid``. Excludes rows with ``is_deleted = 1``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``,
                ``has_new_message`` and ``is_cron``. Flags are SQLite integers.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT conversation_uid, title, work_space, last_active_at,
                   created_at, latest_cursor, is_pinned, has_new_message,
                   is_cron
            FROM conversations
            WHERE user_uid = ? AND is_deleted != 1
            ORDER BY is_pinned DESC, last_active_at DESC
            """,
            (str(payload["user_uid"]),),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_conversation_meta_by_id(self, payload: dict) -> dict:
        """Get metadata for one conversation that is not marked deleted.

        Args:
            payload: Required ``conversation_uid``; no user filter is applied.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one row.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``
                and ``has_new_message``. Flags are SQLite integers.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT conversation_uid, title, work_space, last_active_at,
                   created_at, latest_cursor, is_pinned, has_new_message
            FROM conversations
            WHERE conversation_uid = ? AND is_deleted != 1
            LIMIT 1
            """,
            (payload["conversation_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def create_conversation(self, payload: dict) -> dict:
        """Create a conversation with a new UUID4 string ID.

        Args:
            payload: Required ``user_uid`` for an existing user. Optional fields:
                ``platform`` defaults to ``"default"`` when absent; ``title``
                defaults to ``"New Conversation..."`` when absent or ``None``;
                falsey ``workspace`` becomes ``""``; ``is_cron`` defaults to
                ``False``. The initial cursor is zero.

        Returns:
            Success: ``{"success": True, "messages": {"conversation_uid": str}}``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        conversation_uid = self._conversation_id_generator()
        await self._execute(
            """
            INSERT INTO conversations (
                user_uid, platform, conversation_uid, title, work_space,
                last_active_at, latest_cursor, is_cron
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, ?)
            """,
            (
                payload["user_uid"],
                payload.get("platform", "default"),
                conversation_uid,
                (
                    "New Conversation..."
                    if payload.get("title") is None
                    else payload["title"]
                ),
                payload.get("workspace") or "",
                payload.get("is_cron", False),
            ),
        )
        return {"success": True, "messages": {"conversation_uid": conversation_uid}}

    @service_handler
    async def update_conversation(self, payload: dict) -> dict:
        """Partially update a conversation owned by a user.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``. Optional
                ``title``, ``workspace``, ``is_pinned``, ``is_deleted`` and
                ``has_new_message``; absent or ``None`` values leave fields unchanged.
                Deleted rows can also be updated or restored by this method.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                conversation matches. Does not update activity timestamps.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        conversation_uid = payload["conversation_uid"]
        await self._execute(
            """
            UPDATE conversations
            SET title = COALESCE(?, title),
                work_space = COALESCE(?, work_space),
                is_pinned = COALESCE(?, is_pinned),
                is_deleted = COALESCE(?, is_deleted),
                has_new_message = COALESCE(?, has_new_message)
            WHERE user_uid = ? AND conversation_uid = ?
            """,
            (
                payload.get("title"),
                payload.get("workspace"),
                payload.get("is_pinned"),
                payload.get("is_deleted"),
                payload.get("has_new_message"),
                payload["user_uid"],
                conversation_uid,
            ),
        )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Messages
    # --------------------------------------------------

    @service_handler
    async def append_message(self, payload: dict) -> dict:
        """Atomically insert one message and advance its conversation cursor.

        Args:
            payload: Required ``user_uid``, ``conversation_uid`` and a nonempty
                ``message`` dictionary. The conversation must belong to the user
                and have ``is_deleted = 0``.
                ``message`` requires a unique ``message_uid`` and ``role`` from
                ``user``, ``assistant``, ``system``, ``tool`` or ``info``.
                Optional ``generation_id``, ``node_id`` and ``parent_id`` default
                to ``""``. ``name``, ``content``, ``tool_calls``, ``tool_call_id``,
                ``refusal``, ``reasoning_content``, ``audio``, ``annotations`` and
                ``function_call`` default to ``None``. Content can be any
                JSON-serializable value; tool calls/annotations must be arrays
                and audio/function_call objects when present.
                ``metadata`` and ``extensions`` default to empty objects, including
                for ``None``; strings are used as JSON text and other values are
                JSON-encoded. The schema enforces JSON validity.

        Returns:
            Success: ``{"success": True, "messages": {"msg_cursor": int, "timestamp": str}}``.
                The timestamp uses a ``T`` separator. Updates ``latest_cursor``
                and ``last_active_at``; ``has_new_message`` is zero for user
                messages and one for other roles.
            Failure: ``{"success": False, "messages": "fail: <error>"}``;
                the insert and conversation update are rolled back together.
        """
        user_uid = payload["user_uid"]
        conversation_uid = payload["conversation_uid"]
        message = payload["message"]
        if not message:
            raise ValueError("Messages list is empty")

        role = message["role"]
        metadata = self._json(message.get("metadata"), {})
        extensions = self._json(message.get("extensions"), {})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute("BEGIN IMMEDIATE")
            conversation = connection.execute(
                """
                SELECT id, latest_cursor
                FROM conversations
                WHERE user_uid = ? AND conversation_uid = ?
                  AND is_deleted = 0
                """,
                (user_uid, conversation_uid),
            ).fetchone()
            if conversation is None:
                raise RuntimeError("Conversation not found or deleted")

            cursor = int(conversation["latest_cursor"]) + 1
            insert_cursor = connection.execute(
                """
                INSERT INTO messages (
                    message_uid, msg_cursor, user_uid, conversation_id,
                    conversation_uid, generation_id, node_id, parent_id,
                    role, name, content, tool_calls, tool_call_id, refusal,
                    reasoning_content, audio, annotations, function_call, metadata, extensions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message["message_uid"],
                    cursor,
                    user_uid,
                    conversation["id"],
                    conversation_uid,
                    message.get("generation_id", ""),
                    message.get("node_id", ""),
                    message.get("parent_id", ""),
                    role,
                    message.get("name"),
                    encode_content(message.get("content")),
                    encode_optional(message.get("tool_calls")),
                    message.get("tool_call_id"),
                    message.get("refusal"),
                    message.get("reasoning_content"),
                    encode_optional(message.get("audio")),
                    encode_optional(message.get("annotations")),
                    encode_optional(message.get("function_call")),
                    metadata,
                    extensions,
                ),
            )
            connection.execute(
                """
                UPDATE conversations
                SET latest_cursor = ?,
                    last_active_at = CURRENT_TIMESTAMP,
                    has_new_message = ?
                WHERE id = ?
                """,
                (
                    cursor,
                    0 if role == "user" else 1,
                    conversation["id"],
                ),
            )
            created = connection.execute(
                "SELECT timestamp FROM messages WHERE id = ?",
                (insert_cursor.lastrowid,),
            ).fetchone()
            result = {
                "msg_cursor": cursor,
                "timestamp": created["timestamp"],
            }
            return self._normalize_row(result)

        result = await self._run(operation)
        return {"success": True, "messages": result}

    @service_handler
    async def delete_messages(self, payload: dict) -> dict:
        """Soft-delete every message matching any supplied node ID.

        Args:
            payload: Required ``user_uid``, ``conversation_uid`` and nonempty
                ``messages`` (a list of node IDs, not message IDs). Matches are
                scoped to the user and conversation, including already deleted
                messages. Repeated node IDs may repeat returned message IDs.

        Returns:
            Success: ``{"success": True, "messages": [{"message_uid": str}, ...]}``.
                Only truthy stored message IDs are returned. Unmatched node IDs
                contribute no rows; empty input is a failure.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        node_ids = payload["messages"]
        if not node_ids:
            raise ValueError("Messages list is empty")
        user_uid = payload["user_uid"]
        conversation_uid = payload["conversation_uid"]

        def operation(connection: sqlite3.Connection) -> list[dict]:
            result: list[dict] = []
            for node_id in node_ids:
                rows = connection.execute(
                    """
                    SELECT id, message_uid FROM messages
                    WHERE user_uid = ? AND conversation_uid = ? AND node_id = ?
                    """,
                    (user_uid, conversation_uid, node_id),
                ).fetchall()
                if rows:
                    connection.executemany(
                        "UPDATE messages SET is_deleted = 1 WHERE id = ?",
                        [(row["id"],) for row in rows],
                    )
                result.extend(
                    {"message_uid": row["message_uid"]}
                    for row in rows
                    if row["message_uid"]
                )
            return result

        return {"success": True, "messages": await self._run(operation)}

    @service_handler
    async def fetch_messages_after_cursor(self, payload: dict) -> dict:
        """Fetch messages at or after a cursor, including soft-deleted messages.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``. ``cursor``
                defaults to zero, is converted to ``int`` and clamped to zero.
                ``limit`` defaults to 65535 and is converted to ``int``; SQLite
                treats a negative limit as unlimited.

        Returns:
            Success: ``{"success": True, "messages": rows, "next_cursor": int}``.
                Rows follow ascending ``msg_cursor`` order and contain
                ``message_uid``, ``msg_cursor``, ``role``, ``name``, ``content``,
                ``tool_calls``, ``tool_call_id``, ``refusal``, ``reasoning_content``,
                ``audio``, ``annotations``, ``function_call``, ``metadata``,
                ``extensions``, ``timestamp``, ``generation_id``, ``node_id``,
                ``parent_id`` and integer ``is_deleted``. Content, tool calls,
                audio, annotations and function_call are JSON-decoded; metadata
                and extensions remain stored JSON text (or ``None`` for NULL
                columns). Timestamps are strings.
                ``next_cursor`` is the last row's cursor plus one, or the clamped
                input cursor if no rows match.
            Failure: ``{"success": False, "messages": "fail: <error>"}``, with
                no ``next_cursor`` key.
        """
        after_cursor = max(int(payload.get("cursor", 0)), 0)
        limit = int(payload.get("limit", 65535))
        rows = await self._fetch_all(
            """
            SELECT message_uid, msg_cursor, role, name, content, tool_calls,
                   tool_call_id, refusal, reasoning_content, audio, annotations, function_call, metadata,
                   extensions, timestamp, generation_id, node_id, parent_id,
                   is_deleted
            FROM messages
            WHERE user_uid = ? AND conversation_uid = ? AND msg_cursor >= ?
            ORDER BY msg_cursor ASC
            LIMIT ?
            """,
            (
                payload["user_uid"],
                payload["conversation_uid"],
                after_cursor,
                limit,
            ),
        )
        rows = [decode_message_row(row, content_json=True) for row in rows]
        next_cursor = rows[-1]["msg_cursor"] + 1 if rows else after_cursor
        return {
            "success": True,
            "messages": rows,
            "next_cursor": next_cursor,
        }

    @service_handler
    async def search_messages_by_keyword(self, payload: dict) -> dict:
        """Search undeleted user/assistant messages in undeleted conversations.

        Args:
            payload: Required string ``keyword`` and ``user_uid`` for the search.
                Whitespace, underscores and backslashes become LIKE wildcards;
                percent signs remain wildcards. A keyword containing only those
                characters returns an empty success without querying the database.

        Returns:
            Success: ``{"success": True, "messages": rows}``, at most 300 rows,
                ordered by conversation activity descending, then message ID
                descending. Each row has ``conversation_uid``, ``generation_id``,
                ``role``, JSON-decoded ``content``, ``title`` and string
                ``last_active_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        keyword = payload["keyword"]
        if not re.sub(r"[%_\\\s]+", "", keyword):
            return {"success": True, "messages": []}
        keyword = re.sub(r"[_\\\s]+", "%", keyword)
        keyword = re.sub(r"%+", "%", keyword).strip("%")
        rows = await self._fetch_all(
            """
            SELECT m.conversation_uid, m.generation_id, m.role, m.content,
                   c.title, c.last_active_at
            FROM messages AS m
            JOIN conversations AS c ON c.id = m.conversation_id
            WHERE c.user_uid = ? AND c.is_deleted = 0
              AND m.is_deleted = 0 AND m.role IN ('user', 'assistant')
              AND m.content LIKE ?
            ORDER BY c.last_active_at DESC, m.id DESC
            LIMIT 300
            """,
            (payload["user_uid"], f"%{keyword}%"),
        )
        return {"success": True, "messages": [decode_message_row(row, content_json=True) for row in rows]}

    # --------------------------------------------------
    # Skills
    # --------------------------------------------------

    @service_handler
    async def insert_skill_info(self, payload: dict) -> dict:
        """Insert skill metadata for a user.

        Args:
            payload: Required ``user_uid``; optional ``skills`` list (default
                empty). Each skill requires ``skill_id``, ``skill_name``,
                ``skill_description``, ``package_path`` and ``package_size``;
                ``skill_version`` defaults to ``"v1.0"`` when absent and
                ``package_sha256`` defaults to ``None``. IDs must be unique.

        Returns:
            Success: ``{"success": True, "messages": "success"}``; an empty
                skills list is a no-op. New skills are inactive by default.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        user_uid = payload["user_uid"]
        params = []
        for skill in payload.get("skills", []):
            params.append(
                (
                    skill["skill_id"],
                    skill["skill_name"],
                    skill["skill_description"],
                    skill.get("skill_version", "v1.0"),
                    skill["package_path"],
                    skill["package_size"],
                    skill.get("package_sha256"),
                    user_uid,
                )
            )
        if params:
            await self._executemany(
                """
                INSERT INTO agent_skills (
                    skill_id, skill_name, skill_description, skill_version,
                    package_path, package_size, package_sha256, user_uid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        return {"success": True, "messages": "success"}

    @service_handler
    async def update_skill_status(self, payload: dict) -> dict:
        """Change activation or soft-deletion status of an undeleted skill.

        Args:
            payload: Required ``user_uid`` and ``skill_id``. Optional ``is_active``
                and ``deleted`` flags; absent or ``None`` values leave fields
                unchanged. Setting ``deleted`` to one records ``deleted_at``.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted skill matches. This method cannot restore deleted skills.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        await self._execute(
            """
            UPDATE agent_skills
            SET is_active = COALESCE(?, is_active),
                deleted = COALESCE(?, deleted),
                deleted_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP
                                  ELSE deleted_at END
            WHERE skill_id = ? AND user_uid = ? AND deleted = 0
            """,
            (
                payload.get("is_active"),
                payload.get("deleted"),
                payload.get("deleted"),
                payload["skill_id"],
                payload["user_uid"],
            ),
        )
        return {"success": True, "messages": "success"}

    @service_handler
    async def fetch_available_skills(self, payload: dict) -> dict:
        """List undeleted skills, including inactive skills, newest upload first.

        Args:
            payload: Required ``user_uid``; optional ``limit`` (default 5),
                converted to ``int``. Negative limits are unlimited in SQLite.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``skill_id``, ``skill_name``, ``skill_description``,
                ``skill_version``, ``package_path``, ``package_size``, integer
                ``is_active`` and string ``upload_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT skill_id, skill_name, skill_description, skill_version,
                   package_path, package_size, is_active, upload_at
            FROM agent_skills
            WHERE user_uid = ? AND deleted = 0
            ORDER BY upload_at DESC
            LIMIT ?
            """,
            (payload["user_uid"], int(payload.get("limit", 5))),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def fetch_target_skill(self, payload: dict) -> dict:
        """Get one undeleted skill owned by a user.

        Args:
            payload: Required ``user_uid`` and ``skill_id``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one row.
                Rows have ``skill_id``, ``skill_name``, ``skill_description``,
                ``skill_version``, ``package_path``, ``package_size``, integer
                ``is_active``, string ``upload_at``, integer ``deleted`` and
                nullable string ``deleted_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT skill_id, skill_name, skill_description, skill_version,
                   package_path, package_size, is_active, upload_at,
                   deleted, deleted_at
            FROM agent_skills
            WHERE user_uid = ? AND skill_id = ? AND deleted = 0
            LIMIT 1
            """,
            (payload["user_uid"], payload["skill_id"]),
        )
        return {"success": True, "messages": rows}

    # --------------------------------------------------
    # Short-term memory
    # --------------------------------------------------

    @service_handler
    async def fetch_shortterm_memory(self, payload: dict) -> dict:
        """Get only the latest undeleted short-term memory for a conversation.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one row
                containing ``memory_id``, ``content`` and integer
                ``created_timestamp`` (Unix microseconds).
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT memory_id, content, created_timestamp
            FROM shortterm_memory
            WHERE user_uid = ? AND conversation_uid = ? AND is_deleted = 0
            ORDER BY created_timestamp DESC
            LIMIT 1
            """,
            (payload["user_uid"], payload["conversation_uid"]),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def insert_shortterm_memory(self, payload: dict) -> dict:
        """Insert one short-term memory with the current Unix microsecond timestamp.

        Args:
            payload: Required ``memory_id``, ``user_uid``, ``conversation_uid``
                and string ``content``. The memory ID must be unique; user and
                conversation foreign keys must exist.

        Returns:
            Success: ``{"success": True, "messages": "success"}``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        await self._execute(
            """
            INSERT INTO shortterm_memory (
                memory_id, user_uid, conversation_uid, content,
                created_timestamp
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload["memory_id"],
                payload["user_uid"],
                payload["conversation_uid"],
                payload["content"],
                int(time.time() * 1_000_000),
            ),
        )
        return {"success": True, "messages": "success"}

    @service_handler
    async def delete_shortterm_memory(self, payload: dict) -> dict:
        """Soft-delete matching short-term memories in a user's conversation.

        Args:
            payload: Required ``memory_ids`` list. When nonempty, also requires
                ``user_uid`` and ``conversation_uid``. Only undeleted rows match.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, including
                empty input or no matching rows.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        memory_ids = payload["memory_ids"]
        if memory_ids:
            placeholders = ", ".join("?" for _ in memory_ids)
            await self._execute(
                f"""
                UPDATE shortterm_memory SET is_deleted = 1
                WHERE user_uid = ? AND conversation_uid = ?
                  AND is_deleted = 0 AND memory_id IN ({placeholders})
                """,
                (
                    payload["user_uid"],
                    payload["conversation_uid"],
                    *memory_ids,
                ),
            )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Long-term memory
    # --------------------------------------------------

    @service_handler
    async def fetch_longterm_memory(self, payload: dict) -> dict:
        """List a user's undeleted long-term memories, newest memory date first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Each row has ``memory_id``, ``title``, ``date`` (YYYY-MM-DD),
                ``content`` and ``source``. Equal dates sort by row ID descending.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT memory_id, title, memory_date AS date, content, source
            FROM longterm_memory
            WHERE user_uid = ? AND is_deleted = 0
            ORDER BY memory_date DESC, id DESC
            """,
            (payload["user_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def insert_longterm_memory(self, payload: dict) -> dict:
        """Insert one long-term memory for an existing user.

        Args:
            payload: Required ``memory_id``, ``user_uid``, ``title``, ``date``,
                ``content`` and ``source``. IDs must be unique; the schema checks
                ``date`` as YYYY-MM-DD and restricts ``source`` to
                ``"conversation"`` or ``"workspace"``.

        Returns:
            Success: ``{"success": True, "messages": {"memory_id": memory_id}}``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        memory_id = payload["memory_id"]
        await self._execute(
            """
            INSERT INTO longterm_memory (
                memory_id, user_uid, title, memory_date, content, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                payload["user_uid"],
                payload["title"],
                payload["date"],
                payload["content"],
                payload["source"],
            ),
        )
        return {"success": True, "messages": {"memory_id": memory_id}}

    @service_handler
    async def update_longterm_memory(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted long-term memory.

        Args:
            payload: Required ``memory_id`` and ``user_uid``. Optional ``title``,
                ``date``, ``content``, ``source`` and ``is_deleted``; absent or
                ``None`` values leave fields unchanged. Date/source constraints
                match ``insert_longterm_memory``. Deletion records ``deleted_at``.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted memory matches. Matching rows update ``updated_at``;
                deleted memories cannot be restored by this method.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        is_deleted = payload.get("is_deleted")
        await self._execute(
            """
            UPDATE longterm_memory
            SET title = COALESCE(?, title),
                memory_date = COALESCE(?, memory_date),
                content = COALESCE(?, content),
                source = COALESCE(?, source),
                is_deleted = COALESCE(?, is_deleted),
                deleted_at = CASE
                    WHEN ? = 1 THEN CURRENT_TIMESTAMP
                    ELSE deleted_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE memory_id = ? AND user_uid = ? AND is_deleted = 0
            """,
            (
                payload.get("title"),
                payload.get("date"),
                payload.get("content"),
                payload.get("source"),
                is_deleted,
                is_deleted,
                payload["memory_id"],
                payload["user_uid"],
            ),
        )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # LLM providers
    # --------------------------------------------------

    @service_handler
    async def create_llm_provider(self, payload: dict) -> dict:
        """Insert an LLM provider for an existing user.

        Args:
            payload: Required ``provider_id``, ``user_uid``, ``provider_name``,
                ``endpoint`` and JSON-serializable ``model_list`` (normally a list).
                ``type`` defaults to ``"openai"`` when absent or falsey and is
                lowercased; the schema permits ``openai`` and ``anthropic``.
                ``description`` defaults to ``None``. The provider ID must be unique.

        Returns:
            Success: ``{"success": True, "messages": {"provider_id": provider_id}}``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        provider_id = payload["provider_id"]
        await self._execute(
            """
            INSERT INTO llm_provider (
                provider_id, user_uid, provider_name, type, endpoint,
                model_list, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                payload["user_uid"],
                payload["provider_name"],
                (payload.get("type", "openai") or "openai").lower(),
                payload["endpoint"],
                json.dumps(payload["model_list"]),
                payload.get("description"),
            ),
        )
        return {"success": True, "messages": {"provider_id": provider_id}}

    @service_handler
    async def get_llm_providers(self, payload: dict) -> dict:
        """List a user's undeleted LLM providers, newest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``provider_id``, ``provider_name``, ``type``, ``endpoint``,
                ``model_list`` (JSON text), nullable ``description`` and string
                ``created_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT provider_id, provider_name, type, endpoint, model_list,
                   description, created_at
            FROM llm_provider
            WHERE user_uid = ? AND is_deleted = 0
            ORDER BY created_at DESC
            """,
            (payload["user_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_llm_provider_by_id(self, payload: dict) -> dict:
        """Get one undeleted LLM provider by ID, without a user filter.

        Args:
            payload: Required ``provider_id``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one row
                shaped as in ``get_llm_providers``; ``model_list`` is JSON text.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT provider_id, provider_name, type, endpoint, model_list,
                   description, created_at
            FROM llm_provider
            WHERE provider_id = ? AND is_deleted = 0
            LIMIT 1
            """,
            (payload["provider_id"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def update_llm_provider(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted LLM provider.

        Args:
            payload: Required ``provider_id`` and ``user_uid``. Optional
                ``provider_name``, ``type``, ``endpoint``, ``model_list``,
                ``description`` and ``is_deleted``; absent or ``None`` values
                leave fields unchanged. String ``type`` is lowercased. A list
                ``model_list`` is JSON-encoded; JSON text is used as supplied.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted provider matches. Deleted providers cannot be restored.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        provider_type = payload.get("type")
        if isinstance(provider_type, str):
            provider_type = provider_type.lower()
        model_list = payload.get("model_list")
        if isinstance(model_list, list):
            model_list = json.dumps(model_list)
        await self._execute(
            """
            UPDATE llm_provider
            SET provider_name = COALESCE(?, provider_name),
                type = COALESCE(?, type),
                endpoint = COALESCE(?, endpoint),
                model_list = COALESCE(?, model_list),
                description = COALESCE(?, description),
                is_deleted = COALESCE(?, is_deleted)
            WHERE provider_id = ? AND user_uid = ? AND is_deleted = 0
            """,
            (
                payload.get("provider_name"),
                provider_type,
                payload.get("endpoint"),
                model_list,
                payload.get("description"),
                payload.get("is_deleted"),
                payload["provider_id"],
                payload["user_uid"],
            ),
        )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # MCP servers
    # --------------------------------------------------

    @service_handler
    async def create_mcp_server(self, payload: dict) -> dict:
        """Insert MCP server metadata for an existing user.

        Args:
            payload: Required ``mcp_id``, ``user_uid``, ``mcp_name``, ``transport``
                and ``endpoint`` (which may be ``None``). Transport must be
                ``stdio``, ``http``, ``streamable_http``, ``websocket`` or ``sse``.
                Optional JSON-serializable ``config`` defaults to an empty object
                when absent; ``description`` defaults to ``None``. IDs are unique.

        Returns:
            Success: ``{"success": True, "messages": {"mcp_id": mcp_id}}``.
                New servers are enabled with a tool count of zero.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        mcp_id = payload["mcp_id"]
        await self._execute(
            """
            INSERT INTO mcp_server (
                mcp_id, user_uid, mcp_name, transport, endpoint, config,
                description
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mcp_id,
                payload["user_uid"],
                payload["mcp_name"],
                payload["transport"],
                payload["endpoint"],
                json.dumps(payload.get("config", {})),
                payload.get("description"),
            ),
        )
        return {"success": True, "messages": {"mcp_id": mcp_id}}

    @service_handler
    async def get_mcp_servers(self, payload: dict) -> dict:
        """List a user's undeleted MCP servers, newest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint``, JSON-text ``config``, nullable ``description``,
                integer ``enabled`` and ``tool_count``, and string ``created_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT mcp_id, mcp_name, transport, endpoint, config,
                   description, enabled, tool_count, created_at
            FROM mcp_server
            WHERE user_uid = ? AND is_deleted = 0
            ORDER BY created_at DESC
            """,
            (payload["user_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_enabled_mcp_servers(self, payload: dict) -> dict:
        """List a user's enabled, undeleted MCP servers, oldest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint`` and JSON-text ``config``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT mcp_id, mcp_name, transport, endpoint, config
            FROM mcp_server
            WHERE user_uid = ? AND enabled = 1 AND is_deleted = 0
            ORDER BY created_at ASC
            """,
            (payload["user_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def update_mcp_server(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted MCP server.

        Args:
            payload: Required ``mcp_id`` and ``user_uid``. Optional ``mcp_name``,
                ``transport``, ``endpoint``, ``config``, ``description``, ``enabled``,
                ``tool_count`` and ``is_deleted``; absent or ``None`` values leave
                fields unchanged. Dict/list configs are JSON-encoded; JSON text
                is used as supplied. Transport retains its schema constraints.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted server matches. Deleted servers cannot be restored.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        config = payload.get("config")
        if isinstance(config, (dict, list)):
            config = json.dumps(config)
        await self._execute(
            """
            UPDATE mcp_server
            SET mcp_name = COALESCE(?, mcp_name),
                transport = COALESCE(?, transport),
                endpoint = COALESCE(?, endpoint),
                config = COALESCE(?, config),
                description = COALESCE(?, description),
                enabled = COALESCE(?, enabled),
                tool_count = COALESCE(?, tool_count),
                is_deleted = COALESCE(?, is_deleted)
            WHERE mcp_id = ? AND user_uid = ? AND is_deleted = 0
            """,
            (
                payload.get("mcp_name"),
                payload.get("transport"),
                payload.get("endpoint"),
                config,
                payload.get("description"),
                payload.get("enabled"),
                payload.get("tool_count"),
                payload.get("is_deleted"),
                payload["mcp_id"],
                payload["user_uid"],
            ),
        )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Cron tasks
    # --------------------------------------------------

    @service_handler
    async def create_cron_task(self, payload: dict) -> dict:
        """Insert a scheduled task for an existing user and conversation.

        Args:
            payload: Required ``task_id`` and ``user_uid`` keys. The schema also
                requires non-None ``conversation_uid``, ``task_name`` and
                ``exec_time``; task IDs must be unique and foreign keys must exist.
                Falsey ``platform`` defaults to ``"default"``; falsey ``repeat``
                defaults to ``"once"``. Repeat permits ``once``, ``day``, ``week``,
                ``month``, ``year`` and ``cron``. ``prompt`` and ``execute`` default
                to ``None``; ``description`` defaults to ``""`` when absent.
                ``extra_config`` defaults to an empty object when absent or
                ``None``; JSON text is used as supplied and other values encoded.
                ``exec_time`` is stored as supplied without date-time validation.

        Returns:
            Success: ``{"success": True, "messages": {"task_id": task_id}}``.
                New tasks are enabled by default.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        task_id = payload["task_id"]
        await self._execute(
            """
            INSERT INTO cron_task (
                task_id, user_uid, conversation_uid, platform, name,
                prompt, execute, exec_time, repeat, extra_config, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                payload["user_uid"],
                payload.get("conversation_uid"),
                payload.get("platform") or "default",
                payload.get("task_name"),
                payload.get("prompt"),
                payload.get("execute"),
                payload.get("exec_time"),
                payload.get("repeat") or "once",
                self._json(payload.get("extra_config"), {}),
                payload.get("description", ""),
            ),
        )
        return {"success": True, "messages": {"task_id": task_id}}

    @service_handler
    async def get_all_enabled_cron_tasks(self, payload: dict) -> dict:
        """List all users' enabled, undeleted tasks by execution time ascending.

        Args:
            payload: Ignored; callers may pass an empty dictionary.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``user_uid``, ``conversation_uid``,
                ``platform``, ``name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, JSON-text ``extra_config``, nullable ``description``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT task_id, user_uid, conversation_uid, platform,
                   name, prompt, execute, exec_time, repeat, extra_config,
                   description, created_at, updated_at
            FROM cron_task
            WHERE enabled = 1 AND is_deleted = 0
            ORDER BY exec_time ASC
            """
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_cron_tasks(self, payload: dict) -> dict:
        """List a user's undeleted tasks by execution time ascending.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``conversation_uid``, ``platform``, ``name``,
                ``prompt``, ``execute``, ``exec_time``, ``repeat``, JSON-text
                ``extra_config``, nullable ``description``, integer ``enabled``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT task_id, conversation_uid, platform, name, prompt,
                   execute, exec_time, repeat, extra_config, description,
                   enabled, created_at, updated_at
            FROM cron_task
            WHERE user_uid = ? AND is_deleted = 0
            ORDER BY exec_time ASC
            """,
            (payload["user_uid"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_cron_task_by_id(self, payload: dict) -> dict:
        """Get one undeleted task by ID, without a user filter.

        Args:
            payload: Required ``task_id``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one
                row shaped as in ``get_cron_tasks`` plus ``user_uid``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            """
            SELECT task_id, user_uid, conversation_uid, platform, name,
                   prompt, execute, exec_time, repeat, extra_config,
                   description, enabled, created_at, updated_at
            FROM cron_task
            WHERE task_id = ? AND is_deleted = 0
            LIMIT 1
            """,
            (payload["task_id"],),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def update_cron_task(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted task by ID.

        Args:
            payload: Required ``task_id``. Optional ``conversation_uid``,
                ``platform``, ``task_name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, ``extra_config``, ``description``, ``enabled`` and
                ``is_deleted``; absent or ``None`` values leave fields unchanged.
                Dict/list configs are JSON-encoded; JSON text is used as supplied.
                The repeat/foreign-key constraints still apply. No user filter
                or date-time validation is applied.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted task matches. Matching rows update ``updated_at``;
                deleted tasks cannot be restored by this method.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        extra_config = payload.get("extra_config")
        if isinstance(extra_config, (dict, list)):
            extra_config = json.dumps(extra_config)
        await self._execute(
            """
            UPDATE cron_task
            SET conversation_uid = COALESCE(?, conversation_uid),
                platform = COALESCE(?, platform),
                name = COALESCE(?, name),
                prompt = COALESCE(?, prompt),
                execute = COALESCE(?, execute),
                exec_time = COALESCE(?, exec_time),
                repeat = COALESCE(?, repeat),
                extra_config = COALESCE(?, extra_config),
                description = COALESCE(?, description),
                enabled = COALESCE(?, enabled),
                is_deleted = COALESCE(?, is_deleted),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND is_deleted = 0
            """,
            (
                payload.get("conversation_uid"),
                payload.get("platform"),
                payload.get("task_name"),
                payload.get("prompt"),
                payload.get("execute"),
                payload.get("exec_time"),
                payload.get("repeat"),
                extra_config,
                payload.get("description"),
                payload.get("enabled"),
                payload.get("is_deleted"),
                payload["task_id"],
            ),
        )
        return {"success": True, "messages": "success"}


data_service = DataService()
auto_init.register(data_service)
