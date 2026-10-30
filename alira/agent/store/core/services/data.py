import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from sqlite3 import SQLITE_BUSY, SQLITE_LOCKED
from typing import Any
from uuid import uuid4

from sqlalchemy import case, event, func, insert, literal, or_, select, update
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql import Executable

from alira.agent.store.core.handlers import service_handler
from alira.agent.store.core.serialization import (
    decode_json_fields,
    decode_message_row,
    encode_json,
    encode_optional_json,
)
from alira.agent.store.core.services.schema import (
    agent_skills,
    conversations,
    cron_task,
    llm_provider,
    longterm_memory,
    mcp_server,
    messages,
    metadata,
    shortterm_memory,
    users,
)
from alira.config.base_config import SQLITE_DATABASE


def _identity_failure(exc: Exception) -> dict:
    return {
        "success": False,
        "messages": {"msg": f"{type(exc).__name__}: {exc}", "uid": None},
    }


async def _database_io[T](operation: Awaitable[T]) -> T:
    """Finish driver I/O before cancellation can roll back or close a connection.

    Cancelling SQLAlchemy's driver await invalidates the connection, which would
    discard an in-memory database. Keep the connection usable for rollback.
    """
    pending = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        finished = asyncio.gather(pending, return_exceptions=True)
        while not finished.done():
            try:
                await asyncio.shield(finished)
            except asyncio.CancelledError:
                continue
        raise


@asynccontextmanager
async def _engine_transaction(
    engine: AsyncEngine, *, immediate: bool = False
) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        if immediate:
            await connection.execution_options(sqlite_begin_immediate=True)
        try:
            await _database_io(connection.begin())
            yield connection
        except BaseException:
            await _database_io(connection.rollback())
            raise
        else:
            await _database_io(connection.commit())


class DataService:
    """SQLite persistence using SQLAlchemy Core and the aiosqlite driver.

    Call ``start()`` before use and ``stop()`` to release the engine. Each
    operation owns a transaction; operations and lifecycle changes are serialized
    within the service, including for an in-memory database. Storage methods
    return result dictionaries; lifecycle errors propagate to the caller.

    Tables, constraints and indexes are defined in ``services/schema.py``. Row
    flags remain integers, time strings use a ``T`` separator, and public reads
    return decoded JSON values.
    """

    def __init__(self, db_path: str | Path | None = None):
        """Select a SQLite file or ``:memory:`` without opening it."""
        selected_path = str(db_path if db_path is not None else SQLITE_DATABASE)
        self._db_path = (
            selected_path
            if selected_path == ":memory:"
            else str(Path(selected_path).expanduser())
        )
        self._engine: AsyncEngine | None = None
        self._lock = asyncio.Lock()

    def _new_conversation_id(self) -> str:
        return str(uuid4())

    async def start(self) -> None:
        """Open the database and initialize its tables; repeated calls are safe."""
        async with self._lock:
            if self._engine is not None:
                return
            if self._db_path != ":memory:":
                Path(self._db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
            engine = create_async_engine(
                URL.create("sqlite+aiosqlite", database=self._db_path),
                hide_parameters=True,
                connect_args={"timeout": 30},
                **(
                    {"pool_size": 1, "max_overflow": 0}
                    if self._db_path != ":memory:"
                    else {}
                ),
            )

            @event.listens_for(engine.sync_engine, "connect")
            def configure_connection(connection, _record) -> None:
                # Let SQLAlchemy control BEGIN so reads and DDL also participate
                # in transactions, instead of sqlite3's legacy transaction mode.
                connection.isolation_level = None
                cursor = connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys = ON")
                    cursor.execute("PRAGMA busy_timeout = 30000")
                    cursor.execute("PRAGMA journal_mode = WAL")
                finally:
                    cursor.close()

            @event.listens_for(engine.sync_engine, "begin")
            def begin_transaction(connection: Connection) -> None:
                connection.exec_driver_sql(
                    "BEGIN IMMEDIATE"
                    if connection.get_execution_options().get("sqlite_begin_immediate")
                    else "BEGIN"
                )

            try:
                for attempt in range(4):
                    try:
                        async with _engine_transaction(
                            engine, immediate=True
                        ) as connection:
                            await _database_io(connection.run_sync(metadata.create_all))
                        break
                    except OperationalError as exc:
                        # Concurrent first connections can race while enabling WAL.
                        # busy_timeout does not wait for every journal-mode change.
                        code = getattr(exc.orig, "sqlite_errorcode", 0)
                        if (
                            code & 0xFF not in {SQLITE_BUSY, SQLITE_LOCKED}
                            or attempt == 3
                        ):
                            raise
                        await asyncio.sleep(0.05 * (2**attempt))
            except BaseException:
                await _database_io(engine.dispose())
                raise
            self._engine = engine

    async def stop(self) -> None:
        """Wait for the active operation, then release connections; restartable."""
        async with self._lock:
            if self._engine is not None:
                try:
                    await _database_io(self._engine.dispose())
                finally:
                    self._engine = None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._lock:
            if self._engine is None:
                raise RuntimeError(
                    "SQLite database is not initialized, call start() first"
                )
            async with _engine_transaction(self._engine) as connection:
                yield connection

    async def _execute(
        self,
        statement: Executable,
        params: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> int:
        async with self._transaction() as connection:
            result = await _database_io(connection.execute(statement, params))
            return result.rowcount

    async def _fetch_all(
        self,
        statement: Executable,
        params: dict[str, Any] | None = None,
        *,
        json_fields: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        async with self._transaction() as connection:
            result = await _database_io(connection.execute(statement, params))
            return [
                decode_json_fields(self._normalize_row(dict(row)), json_fields)
                for row in result.mappings()
            ]

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        """Normalize SQLite timestamps to ISO format."""
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if (key.endswith("_at") or key in {"exec_time", "timestamp"}) and re.match(
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value
            ):
                row[key] = value.replace(" ", "T", 1)
        return row

    # --------------------------------------------------
    # Users
    # --------------------------------------------------

    @service_handler(failure_factory=_identity_failure)
    async def create_user(self, payload: dict) -> dict:
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
            insert(users).values(
                user_uid=user_uid,
                username=payload["username"],
                password=payload["password"],
            )
        )
        return {"success": True, "messages": {"msg": "success", "uid": user_uid}}

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
            select(users.c.user_uid, users.c.username).where(
                users.c.username == payload["username"],
                users.c.password == payload["password"],
            )
        )
        if len(rows) != 1:
            raise ValueError("User do not exist or wrong password.")
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
            select(users.c.user_uid, users.c.username).where(
                or_(
                    users.c.user_uid == payload["user_uid"],
                    users.c.username == literal(payload.get("username")),
                )
            )
        )
        if exist and not rows:
            raise ValueError("User do not exist.")
        if not exist and rows:
            raise ValueError("User has already exist.")
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Conversations
    # --------------------------------------------------

    @service_handler
    async def list_conversations(self, payload: dict) -> dict:
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
            select(
                conversations.c.conversation_uid,
                conversations.c.title,
                conversations.c.work_space,
                conversations.c.last_active_at,
                conversations.c.created_at,
                conversations.c.latest_cursor,
                conversations.c.is_pinned,
                conversations.c.has_new_message,
                conversations.c.is_cron,
            )
            .where(
                conversations.c.user_uid == str(payload["user_uid"]),
                conversations.c.is_deleted != 1,
            )
            .order_by(
                conversations.c.is_pinned.desc(), conversations.c.last_active_at.desc()
            )
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_conversation(self, payload: dict) -> dict:
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
            select(
                conversations.c.conversation_uid,
                conversations.c.title,
                conversations.c.work_space,
                conversations.c.last_active_at,
                conversations.c.created_at,
                conversations.c.latest_cursor,
                conversations.c.is_pinned,
                conversations.c.has_new_message,
            )
            .where(
                conversations.c.conversation_uid == payload["conversation_uid"],
                conversations.c.is_deleted != 1,
            )
            .limit(1)
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
        conversation_uid = self._new_conversation_id()
        await self._execute(
            insert(conversations).values(
                user_uid=payload["user_uid"],
                platform=payload.get("platform", "default"),
                conversation_uid=conversation_uid,
                title="New Conversation..."
                if payload.get("title") is None
                else payload["title"],
                work_space=payload.get("workspace") or "",
                is_cron=payload.get("is_cron", False),
            )
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
        await self._execute(
            update(conversations)
            .where(
                conversations.c.user_uid == payload["user_uid"],
                conversations.c.conversation_uid == payload["conversation_uid"],
            )
            .values(
                title=func.coalesce(payload.get("title"), conversations.c.title),
                work_space=func.coalesce(
                    payload.get("workspace"), conversations.c.work_space
                ),
                is_pinned=func.coalesce(
                    payload.get("is_pinned"), conversations.c.is_pinned
                ),
                is_deleted=func.coalesce(
                    payload.get("is_deleted"), conversations.c.is_deleted
                ),
                has_new_message=func.coalesce(
                    payload.get("has_new_message"), conversations.c.has_new_message
                ),
            )
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
                ``metadata`` and ``extensions`` must be objects and default to
                empty objects, including for ``None``. JSON values are encoded
                by the service, without accepting pre-encoded JSON text.

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
        for field in ("metadata", "extensions"):
            if message.get(field) is not None and not isinstance(message[field], dict):
                raise TypeError(f"Message {field} must be an object")
        role = message["role"]
        async with self._transaction() as connection:
            # Allocate the cursor with the first write. SQLite serializes this
            # UPDATE across engines, avoiding a read-then-write race.
            result = await _database_io(
                connection.execute(
                    update(conversations)
                    .where(
                        conversations.c.user_uid == user_uid,
                        conversations.c.conversation_uid == conversation_uid,
                        conversations.c.is_deleted == 0,
                    )
                    .values(
                        latest_cursor=conversations.c.latest_cursor + 1,
                        last_active_at=func.current_timestamp(),
                        has_new_message=0 if role == "user" else 1,
                    )
                    .returning(conversations.c.id, conversations.c.latest_cursor)
                )
            )
            conversation = result.mappings().one_or_none()
            if conversation is None:
                raise RuntimeError("Conversation not found or deleted")
            cursor = conversation["latest_cursor"]
            result = await _database_io(
                connection.execute(
                    insert(messages)
                    .values(
                        message_uid=message["message_uid"],
                        msg_cursor=cursor,
                        user_uid=user_uid,
                        conversation_id=conversation["id"],
                        conversation_uid=conversation_uid,
                        generation_id=message.get("generation_id", ""),
                        node_id=message.get("node_id", ""),
                        parent_id=message.get("parent_id", ""),
                        role=role,
                        name=message.get("name"),
                        content=encode_json(message.get("content")),
                        tool_calls=encode_optional_json(message.get("tool_calls")),
                        tool_call_id=message.get("tool_call_id"),
                        refusal=message.get("refusal"),
                        reasoning_content=message.get("reasoning_content"),
                        audio=encode_optional_json(message.get("audio")),
                        annotations=encode_optional_json(message.get("annotations")),
                        function_call=encode_optional_json(
                            message.get("function_call")
                        ),
                        metadata=encode_json(message.get("metadata"), default={}),
                        extensions=encode_json(message.get("extensions"), default={}),
                    )
                    .returning(messages.c.timestamp)
                )
            )
            timestamp = result.scalar_one()
        return {
            "success": True,
            "messages": self._normalize_row(
                {
                    "msg_cursor": cursor,
                    "timestamp": timestamp,
                }
            ),
        }

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
        deleted = []
        async with self._transaction() as connection:
            for node_id in node_ids:
                result = await _database_io(
                    connection.execute(
                        update(messages)
                        .where(
                            messages.c.user_uid == user_uid,
                            messages.c.conversation_uid == conversation_uid,
                            messages.c.node_id == node_id,
                        )
                        .values(is_deleted=1)
                        .returning(messages.c.message_uid)
                    )
                )
                deleted.extend({"message_uid": uid} for uid in result.scalars() if uid)
        return {"success": True, "messages": deleted}

    @service_handler
    async def list_messages(self, payload: dict) -> dict:
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
                and extensions are objects, defaulting to empty objects.
                Timestamps are strings.
                ``next_cursor`` is the last row's cursor plus one, or the clamped
                input cursor if no rows match.
            Failure: ``{"success": False, "messages": "fail: <error>"}``, with
                no ``next_cursor`` key.
        """
        after_cursor = max(int(payload.get("cursor", 0)), 0)
        rows = await self._fetch_all(
            select(
                messages.c.message_uid,
                messages.c.msg_cursor,
                messages.c.role,
                messages.c.name,
                messages.c.content,
                messages.c.tool_calls,
                messages.c.tool_call_id,
                messages.c.refusal,
                messages.c.reasoning_content,
                messages.c.audio,
                messages.c.annotations,
                messages.c.function_call,
                messages.c.metadata,
                messages.c.extensions,
                messages.c.timestamp,
                messages.c.generation_id,
                messages.c.node_id,
                messages.c.parent_id,
                messages.c.is_deleted,
            )
            .where(
                messages.c.user_uid == payload["user_uid"],
                messages.c.conversation_uid == payload["conversation_uid"],
                messages.c.msg_cursor >= after_cursor,
            )
            .order_by(messages.c.msg_cursor)
            .limit(int(payload.get("limit", 65535)))
        )
        rows = [decode_message_row(row, content_json=True) for row in rows]
        return {
            "success": True,
            "messages": rows,
            "next_cursor": rows[-1]["msg_cursor"] + 1 if rows else after_cursor,
        }

    @service_handler
    async def search_messages(self, payload: dict) -> dict:
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
            select(
                messages.c.conversation_uid,
                messages.c.generation_id,
                messages.c.role,
                messages.c.content,
                conversations.c.title,
                conversations.c.last_active_at,
            )
            .select_from(
                messages.join(
                    conversations, conversations.c.id == messages.c.conversation_id
                )
            )
            .where(
                conversations.c.user_uid == payload["user_uid"],
                conversations.c.is_deleted == 0,
                messages.c.is_deleted == 0,
                messages.c.role.in_(["user", "assistant"]),
                messages.c.content.like(f"%{keyword}%"),
            )
            .order_by(conversations.c.last_active_at.desc(), messages.c.id.desc())
            .limit(300)
        )
        return {
            "success": True,
            "messages": [decode_message_row(row, content_json=True) for row in rows],
        }

    # --------------------------------------------------
    # Skills
    # --------------------------------------------------

    @service_handler
    async def create_skills(self, payload: dict) -> dict:
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
        params = [
            {
                "skill_id": skill["skill_id"],
                "skill_name": skill["skill_name"],
                "skill_description": skill["skill_description"],
                "skill_version": skill.get("skill_version", "v1.0"),
                "package_path": skill["package_path"],
                "package_size": skill["package_size"],
                "package_sha256": skill.get("package_sha256"),
                "user_uid": user_uid,
            }
            for skill in payload.get("skills", [])
        ]
        if params:
            await self._execute(insert(agent_skills), params)
        return {"success": True, "messages": "success"}

    @service_handler
    async def update_skill(self, payload: dict) -> dict:
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
            update(agent_skills)
            .where(
                agent_skills.c.skill_id == payload["skill_id"],
                agent_skills.c.user_uid == payload["user_uid"],
                agent_skills.c.deleted == 0,
            )
            .values(
                is_active=func.coalesce(
                    payload.get("is_active"), agent_skills.c.is_active
                ),
                deleted=func.coalesce(payload.get("deleted"), agent_skills.c.deleted),
                deleted_at=case(
                    (literal(payload.get("deleted")) == 1, func.current_timestamp()),
                    else_=agent_skills.c.deleted_at,
                ),
            )
        )
        return {"success": True, "messages": "success"}

    @service_handler
    async def list_skills(self, payload: dict) -> dict:
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
            select(
                agent_skills.c.skill_id,
                agent_skills.c.skill_name,
                agent_skills.c.skill_description,
                agent_skills.c.skill_version,
                agent_skills.c.package_path,
                agent_skills.c.package_size,
                agent_skills.c.is_active,
                agent_skills.c.upload_at,
            )
            .where(
                agent_skills.c.user_uid == payload["user_uid"],
                agent_skills.c.deleted == 0,
            )
            .order_by(agent_skills.c.upload_at.desc())
            .limit(int(payload.get("limit", 5)))
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_skill(self, payload: dict) -> dict:
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
            select(
                agent_skills.c.skill_id,
                agent_skills.c.skill_name,
                agent_skills.c.skill_description,
                agent_skills.c.skill_version,
                agent_skills.c.package_path,
                agent_skills.c.package_size,
                agent_skills.c.is_active,
                agent_skills.c.upload_at,
                agent_skills.c.deleted,
                agent_skills.c.deleted_at,
            )
            .where(
                agent_skills.c.user_uid == payload["user_uid"],
                agent_skills.c.skill_id == payload["skill_id"],
                agent_skills.c.deleted == 0,
            )
            .limit(1)
        )
        return {"success": True, "messages": rows}

    # --------------------------------------------------
    # Short-term memory
    # --------------------------------------------------

    @service_handler
    async def list_shortterm_memories(self, payload: dict) -> dict:
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
            select(
                shortterm_memory.c.memory_id,
                shortterm_memory.c.content,
                shortterm_memory.c.created_timestamp,
            )
            .where(
                shortterm_memory.c.user_uid == payload["user_uid"],
                shortterm_memory.c.conversation_uid == payload["conversation_uid"],
                shortterm_memory.c.is_deleted == 0,
            )
            .order_by(shortterm_memory.c.created_timestamp.desc())
            .limit(1)
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def create_shortterm_memory(self, payload: dict) -> dict:
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
            insert(shortterm_memory).values(
                memory_id=payload["memory_id"],
                user_uid=payload["user_uid"],
                conversation_uid=payload["conversation_uid"],
                content=payload["content"],
                created_timestamp=int(time.time() * 1_000_000),
            )
        )
        return {"success": True, "messages": "success"}

    @service_handler
    async def delete_shortterm_memories(self, payload: dict) -> dict:
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
            await self._execute(
                update(shortterm_memory)
                .where(
                    shortterm_memory.c.user_uid == payload["user_uid"],
                    shortterm_memory.c.conversation_uid == payload["conversation_uid"],
                    shortterm_memory.c.is_deleted == 0,
                    shortterm_memory.c.memory_id.in_(memory_ids),
                )
                .values(is_deleted=1)
            )
        return {"success": True, "messages": "success"}

    # --------------------------------------------------
    # Long-term memory
    # --------------------------------------------------

    @service_handler
    async def list_longterm_memories(self, payload: dict) -> dict:
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
            select(
                longterm_memory.c.memory_id,
                longterm_memory.c.title,
                longterm_memory.c.memory_date.label("date"),
                longterm_memory.c.content,
                longterm_memory.c.source,
            )
            .where(
                longterm_memory.c.user_uid == payload["user_uid"],
                longterm_memory.c.is_deleted == 0,
            )
            .order_by(longterm_memory.c.memory_date.desc(), longterm_memory.c.id.desc())
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def create_longterm_memory(self, payload: dict) -> dict:
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
            insert(longterm_memory).values(
                memory_id=memory_id,
                user_uid=payload["user_uid"],
                title=payload["title"],
                memory_date=payload["date"],
                content=payload["content"],
                source=payload["source"],
            )
        )
        return {"success": True, "messages": {"memory_id": memory_id}}

    @service_handler
    async def update_longterm_memory(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted long-term memory.

        Args:
            payload: Required ``memory_id`` and ``user_uid``. Optional ``title``,
                ``date``, ``content``, ``source`` and ``is_deleted``; absent or
                ``None`` values leave fields unchanged. Date/source constraints
                match ``create_longterm_memory``. Deletion records ``deleted_at``.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted memory matches. Matching rows update ``updated_at``;
                deleted memories cannot be restored by this method.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        is_deleted = payload.get("is_deleted")
        await self._execute(
            update(longterm_memory)
            .where(
                longterm_memory.c.memory_id == payload["memory_id"],
                longterm_memory.c.user_uid == payload["user_uid"],
                longterm_memory.c.is_deleted == 0,
            )
            .values(
                title=func.coalesce(payload.get("title"), longterm_memory.c.title),
                memory_date=func.coalesce(
                    payload.get("date"), longterm_memory.c.memory_date
                ),
                content=func.coalesce(
                    payload.get("content"), longterm_memory.c.content
                ),
                source=func.coalesce(payload.get("source"), longterm_memory.c.source),
                is_deleted=func.coalesce(is_deleted, longterm_memory.c.is_deleted),
                deleted_at=case(
                    (literal(is_deleted) == 1, func.current_timestamp()),
                    else_=longterm_memory.c.deleted_at,
                ),
                updated_at=func.current_timestamp(),
            )
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
            insert(llm_provider).values(
                provider_id=provider_id,
                user_uid=payload["user_uid"],
                provider_name=payload["provider_name"],
                type=(payload.get("type", "openai") or "openai").lower(),
                endpoint=payload["endpoint"],
                model_list=encode_json(payload["model_list"]),
                description=payload.get("description"),
            )
        )
        return {"success": True, "messages": {"provider_id": provider_id}}

    @service_handler
    async def list_llm_providers(self, payload: dict) -> dict:
        """List a user's undeleted LLM providers, newest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``provider_id``, ``provider_name``, ``type``, ``endpoint``,
                decoded ``model_list``, nullable ``description`` and string
                ``created_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                llm_provider.c.provider_id,
                llm_provider.c.provider_name,
                llm_provider.c.type,
                llm_provider.c.endpoint,
                llm_provider.c.model_list,
                llm_provider.c.description,
                llm_provider.c.created_at,
            )
            .where(
                llm_provider.c.user_uid == payload["user_uid"],
                llm_provider.c.is_deleted == 0,
            )
            .order_by(llm_provider.c.created_at.desc()),
            json_fields=("model_list",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_llm_provider(self, payload: dict) -> dict:
        """Get one undeleted LLM provider by ID, without a user filter.

        Args:
            payload: Required ``provider_id``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one row
                shaped as in ``list_llm_providers``; ``model_list`` is decoded.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                llm_provider.c.provider_id,
                llm_provider.c.provider_name,
                llm_provider.c.type,
                llm_provider.c.endpoint,
                llm_provider.c.model_list,
                llm_provider.c.description,
                llm_provider.c.created_at,
            )
            .where(
                llm_provider.c.provider_id == payload["provider_id"],
                llm_provider.c.is_deleted == 0,
            )
            .limit(1),
            json_fields=("model_list",),
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
                ``model_list`` is encoded as a native JSON value.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted provider matches. Deleted providers cannot be restored.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        provider_type = payload.get("type")
        if isinstance(provider_type, str):
            provider_type = provider_type.lower()
        model_list = encode_optional_json(payload.get("model_list"))
        await self._execute(
            update(llm_provider)
            .where(
                llm_provider.c.provider_id == payload["provider_id"],
                llm_provider.c.user_uid == payload["user_uid"],
                llm_provider.c.is_deleted == 0,
            )
            .values(
                provider_name=func.coalesce(
                    payload.get("provider_name"), llm_provider.c.provider_name
                ),
                type=func.coalesce(provider_type, llm_provider.c.type),
                endpoint=func.coalesce(
                    payload.get("endpoint"), llm_provider.c.endpoint
                ),
                model_list=func.coalesce(model_list, llm_provider.c.model_list),
                description=func.coalesce(
                    payload.get("description"), llm_provider.c.description
                ),
                is_deleted=func.coalesce(
                    payload.get("is_deleted"), llm_provider.c.is_deleted
                ),
            )
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
            insert(mcp_server).values(
                mcp_id=mcp_id,
                user_uid=payload["user_uid"],
                mcp_name=payload["mcp_name"],
                transport=payload["transport"],
                endpoint=payload["endpoint"],
                config=encode_json(payload.get("config", {})),
                description=payload.get("description"),
            )
        )
        return {"success": True, "messages": {"mcp_id": mcp_id}}

    @service_handler
    async def list_mcp_servers(self, payload: dict) -> dict:
        """List a user's undeleted MCP servers, newest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint``, decoded ``config``, nullable ``description``,
                integer ``enabled`` and ``tool_count``, and string ``created_at``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                mcp_server.c.mcp_id,
                mcp_server.c.mcp_name,
                mcp_server.c.transport,
                mcp_server.c.endpoint,
                mcp_server.c.config,
                mcp_server.c.description,
                mcp_server.c.enabled,
                mcp_server.c.tool_count,
                mcp_server.c.created_at,
            )
            .where(
                mcp_server.c.user_uid == payload["user_uid"],
                mcp_server.c.is_deleted == 0,
            )
            .order_by(mcp_server.c.created_at.desc()),
            json_fields=("config",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def list_enabled_mcp_servers(self, payload: dict) -> dict:
        """List a user's enabled, undeleted MCP servers, oldest creation first.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint`` and decoded ``config``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                mcp_server.c.mcp_id,
                mcp_server.c.mcp_name,
                mcp_server.c.transport,
                mcp_server.c.endpoint,
                mcp_server.c.config,
            )
            .where(
                mcp_server.c.user_uid == payload["user_uid"],
                mcp_server.c.enabled == 1,
                mcp_server.c.is_deleted == 0,
            )
            .order_by(mcp_server.c.created_at.asc()),
            json_fields=("config",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def update_mcp_server(self, payload: dict) -> dict:
        """Partially update or soft-delete an undeleted MCP server.

        Args:
            payload: Required ``mcp_id`` and ``user_uid``. Optional ``mcp_name``,
                ``transport``, ``endpoint``, ``config``, ``description``, ``enabled``,
                ``tool_count`` and ``is_deleted``; absent or ``None`` values leave
                fields unchanged. Config is encoded as a native JSON value.
                Transport retains its schema constraints.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted server matches. Deleted servers cannot be restored.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        config = encode_optional_json(payload.get("config"))
        await self._execute(
            update(mcp_server)
            .where(
                mcp_server.c.mcp_id == payload["mcp_id"],
                mcp_server.c.user_uid == payload["user_uid"],
                mcp_server.c.is_deleted == 0,
            )
            .values(
                mcp_name=func.coalesce(payload.get("mcp_name"), mcp_server.c.mcp_name),
                transport=func.coalesce(
                    payload.get("transport"), mcp_server.c.transport
                ),
                endpoint=func.coalesce(payload.get("endpoint"), mcp_server.c.endpoint),
                config=func.coalesce(config, mcp_server.c.config),
                description=func.coalesce(
                    payload.get("description"), mcp_server.c.description
                ),
                enabled=func.coalesce(payload.get("enabled"), mcp_server.c.enabled),
                tool_count=func.coalesce(
                    payload.get("tool_count"), mcp_server.c.tool_count
                ),
                is_deleted=func.coalesce(
                    payload.get("is_deleted"), mcp_server.c.is_deleted
                ),
            )
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
                ``None``; otherwise it is encoded as a native JSON value.
                ``exec_time`` is stored as supplied without date-time validation.

        Returns:
            Success: ``{"success": True, "messages": {"task_id": task_id}}``.
                New tasks are enabled by default.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        task_id = payload["task_id"]
        await self._execute(
            insert(cron_task).values(
                task_id=task_id,
                user_uid=payload["user_uid"],
                conversation_uid=payload.get("conversation_uid"),
                platform=payload.get("platform") or "default",
                name=payload.get("task_name"),
                prompt=payload.get("prompt"),
                execute=payload.get("execute"),
                exec_time=payload.get("exec_time"),
                repeat=payload.get("repeat") or "once",
                extra_config=encode_json(payload.get("extra_config"), default={}),
                description=payload.get("description", ""),
            )
        )
        return {"success": True, "messages": {"task_id": task_id}}

    @service_handler
    async def list_enabled_cron_tasks(self, payload: dict) -> dict:
        """List all users' enabled, undeleted tasks by execution time ascending.

        Args:
            payload: Ignored; callers may pass an empty dictionary.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``user_uid``, ``conversation_uid``,
                ``platform``, ``name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, decoded ``extra_config``, nullable ``description``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                cron_task.c.task_id,
                cron_task.c.user_uid,
                cron_task.c.conversation_uid,
                cron_task.c.platform,
                cron_task.c.name,
                cron_task.c.prompt,
                cron_task.c.execute,
                cron_task.c.exec_time,
                cron_task.c.repeat,
                cron_task.c.extra_config,
                cron_task.c.description,
                cron_task.c.created_at,
                cron_task.c.updated_at,
            )
            .where(cron_task.c.enabled == 1, cron_task.c.is_deleted == 0)
            .order_by(cron_task.c.exec_time.asc()),
            json_fields=("extra_config",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def list_cron_tasks(self, payload: dict) -> dict:
        """List a user's undeleted tasks by execution time ascending.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``conversation_uid``, ``platform``, ``name``,
                ``prompt``, ``execute``, ``exec_time``, ``repeat``, decoded
                ``extra_config``, nullable ``description``, integer ``enabled``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                cron_task.c.task_id,
                cron_task.c.conversation_uid,
                cron_task.c.platform,
                cron_task.c.name,
                cron_task.c.prompt,
                cron_task.c.execute,
                cron_task.c.exec_time,
                cron_task.c.repeat,
                cron_task.c.extra_config,
                cron_task.c.description,
                cron_task.c.enabled,
                cron_task.c.created_at,
                cron_task.c.updated_at,
            )
            .where(
                cron_task.c.user_uid == payload["user_uid"], cron_task.c.is_deleted == 0
            )
            .order_by(cron_task.c.exec_time.asc()),
            json_fields=("extra_config",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_cron_task(self, payload: dict) -> dict:
        """Get one undeleted task by ID, without a user filter.

        Args:
            payload: Required ``task_id``.

        Returns:
            Success: ``{"success": True, "messages": rows}``, with zero or one
                row shaped as in ``list_cron_tasks`` plus ``user_uid``.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        rows = await self._fetch_all(
            select(
                cron_task.c.task_id,
                cron_task.c.user_uid,
                cron_task.c.conversation_uid,
                cron_task.c.platform,
                cron_task.c.name,
                cron_task.c.prompt,
                cron_task.c.execute,
                cron_task.c.exec_time,
                cron_task.c.repeat,
                cron_task.c.extra_config,
                cron_task.c.description,
                cron_task.c.enabled,
                cron_task.c.created_at,
                cron_task.c.updated_at,
            )
            .where(
                cron_task.c.task_id == payload["task_id"], cron_task.c.is_deleted == 0
            )
            .limit(1),
            json_fields=("extra_config",),
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
                Config is encoded as a native JSON value.
                The repeat/foreign-key constraints still apply. No user filter
                or date-time validation is applied.

        Returns:
            Success: ``{"success": True, "messages": "success"}``, even if no
                undeleted task matches. Matching rows update ``updated_at``;
                deleted tasks cannot be restored by this method.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        extra_config = encode_optional_json(payload.get("extra_config"))
        await self._execute(
            update(cron_task)
            .where(
                cron_task.c.task_id == payload["task_id"], cron_task.c.is_deleted == 0
            )
            .values(
                conversation_uid=func.coalesce(
                    payload.get("conversation_uid"), cron_task.c.conversation_uid
                ),
                platform=func.coalesce(payload.get("platform"), cron_task.c.platform),
                name=func.coalesce(payload.get("task_name"), cron_task.c.name),
                prompt=func.coalesce(payload.get("prompt"), cron_task.c.prompt),
                execute=func.coalesce(payload.get("execute"), cron_task.c.execute),
                exec_time=func.coalesce(
                    payload.get("exec_time"), cron_task.c.exec_time
                ),
                repeat=func.coalesce(payload.get("repeat"), cron_task.c.repeat),
                extra_config=func.coalesce(extra_config, cron_task.c.extra_config),
                description=func.coalesce(
                    payload.get("description"), cron_task.c.description
                ),
                enabled=func.coalesce(payload.get("enabled"), cron_task.c.enabled),
                is_deleted=func.coalesce(
                    payload.get("is_deleted"), cron_task.c.is_deleted
                ),
                updated_at=func.current_timestamp(),
            )
        )
        return {"success": True, "messages": "success"}
