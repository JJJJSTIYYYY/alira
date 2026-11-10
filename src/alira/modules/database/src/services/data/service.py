"""Business reads and writes backed by the SQLite data service."""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from my_llm.middleware.validation import validate_messages
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    case,
    delete,
    func,
    insert,
    inspect,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import ColumnElement, Executable, Select

from alira.modules.database.src.services.data.embedding import (
    MessageEmbedding,
    configure_vector_engine,
)
from alira.modules.database.src.services.data.engine import (
    create_engine,
    database_io,
    engine_transaction,
    initialize_schema,
)
from alira.modules.database.src.services.data.schema import (
    compressed_context,
    conversations,
    cron_task,
    llm_provider,
    mcp_server,
    messages,
    users,
)
from alira.modules.database.src.utils.errors import (
    ERROR_MESSAGES,
    StoreCode,
    business_error_result,
    exception_code,
)
from alira.modules.database.src.utils.handlers import service_handler
from alira.modules.database.src.utils.serialization import (
    decode_json_fields,
    decode_message_row,
    encode_json,
    encode_optional_json,
    normalize_row,
)
from alira.modules.database.config import (
    SQLITE_DATABASE,
    SQLITE_EMBEDDING_ENABLE,
    SQLITE_EMBEDDING_MODEL,
    SQLITE_EMBEDDING_OPTIONS,
    VECTOR_DIMENSION,
)
from alira.modules.agent.src.shared.message import message_wire_from_record
from alira.shared.logger import logger

MESSAGE_RECORD_COLUMNS = tuple(
    column for column in messages.c if column.name not in {"id", "conversation_id"}
)


class DataService:
    """SQLite persistence using SQLAlchemy Core and the aiosqlite driver.

    Call ``start()`` before use and ``stop()`` to release the engine. Each
    database operation owns a transaction; transactions and lifecycle changes are
    serialized within the service, including for an in-memory database. Storage methods
    return result dictionaries; lifecycle errors propagate to the caller.

    Business tables are defined in ``data/schema.py``; model-specific vector
    tables are defined in ``data/embedding.py``. Row
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
        self._embedding: MessageEmbedding | None = None
        self._embedding_tasks: set[asyncio.Task[None]] = set()
        self._embedding_sync_task: asyncio.Task[None] | None = None
        self._embedding_sync_status: dict[str, Any] | None = None

    def _new_conversation_id(self) -> str:
        return str(uuid4())

    async def start(self) -> None:
        """Initialize tables without backfilling embeddings; repeated calls are safe."""
        async with self._lock:
            if self._engine is not None:
                return
            if self._db_path != ":memory:":
                Path(self._db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(self._db_path)
            try:
                embedding = (
                    MessageEmbedding(
                        SQLITE_EMBEDDING_MODEL,
                        VECTOR_DIMENSION,
                        **SQLITE_EMBEDDING_OPTIONS,
                    )
                    if SQLITE_EMBEDDING_ENABLE
                    else None
                )
                if embedding is not None:
                    configure_vector_engine(engine)
                await initialize_schema(engine)
                if embedding is not None:
                    async with engine_transaction(engine) as connection:
                        await database_io(
                            connection.run_sync(embedding.table.create, checkfirst=True)
                        )
            except BaseException:
                await database_io(engine.dispose())
                raise
            self._engine = engine
            self._embedding = embedding
            self._embedding_sync_task = None
            self._embedding_sync_status = None

    async def stop(self) -> None:
        """Drain background indexing and release the engine/model; restartable."""
        if self._embedding_sync_task is not None:
            await database_io(self._embedding_sync_task)
        await self._wait_for_embedding_tasks()
        async with self._lock:
            if self._engine is not None:
                try:
                    await database_io(self._engine.dispose())
                finally:
                    self._engine = None
                    if self._embedding is not None:
                        await self._embedding.close()
                        self._embedding = None

    async def _wait_for_embedding_tasks(self) -> None:
        if self._embedding_tasks:
            await database_io(
                asyncio.gather(*self._embedding_tasks, return_exceptions=True)
            )

    def _schedule_embedding(self, message_id: int) -> None:
        task = asyncio.create_task(self._index_message(message_id))
        self._embedding_tasks.add(task)
        task.add_done_callback(self._embedding_done)

    def _embedding_done(self, task: asyncio.Task[None]) -> None:
        self._embedding_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Message embedding failed: %s", error)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._lock:
            if self._engine is None:
                raise RuntimeError(
                    "SQLite database is not initialized, call start() first"
                )
            async with engine_transaction(self._engine) as connection:
                yield connection

    async def _execute(
        self,
        statement: Executable,
        params: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> int:
        async with self._transaction() as connection:
            result = await database_io(connection.execute(statement, params))
            return result.rowcount

    async def _fetch_all(
        self,
        statement: Executable,
        params: dict[str, Any] | None = None,
        *,
        json_fields: tuple[str, ...] = (),
        row_decoder: Callable[[dict], dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Materialize, normalize and decode each row in one pass."""
        async with self._transaction() as connection:
            result = await database_io(connection.execute(statement, params))
            rows = []
            for row in result.mappings():
                record = decode_json_fields(normalize_row(dict(row)), json_fields)
                if row_decoder is not None:
                    record = row_decoder(record)
                rows.append(record)
            return rows

    # --------------------------------------------------
    # Users
    # --------------------------------------------------

    @service_handler
    async def create_user(self, payload: dict) -> dict:
        """Insert an account using the UID generated by StoreExecutor.

        Args:
            payload: Required nonempty strings ``user_uid`` and ``user_name``.
                Both columns are unique. No password is stored.

        Returns:
            ``{"success": True, "code": 0, "messages": {"user_uid": str,
            "user_name": str}}``. Duplicate names return code 2002; other
            business errors return success=True with a nonzero code and a standard
            message; unexpected exceptions return success=False.
        """
        user_uid = payload["user_uid"]
        user_name = payload["user_name"]
        statement = (
            sqlite_insert(users)
            .values(user_uid=user_uid, user_name=user_name)
            .on_conflict_do_nothing(index_elements=[users.c.user_name])
            .returning(users.c.user_uid, users.c.user_name)
        )
        rows = await self._fetch_all(statement)
        if not rows:
            return business_error_result(StoreCode.USER_ALREADY_EXISTS)
        return {"success": True, "messages": rows[0]}

    @service_handler
    async def verify_user(self, payload: dict) -> dict:
        """Resolve an existing user by UID, name, or both.

        Args:
            payload: At least one of ``user_uid`` and ``user_name``, each a
                nonempty string when supplied. If both are supplied they must
                identify the same row; no password is required.

        Returns:
            ``{"success": True, "code": 0, "messages": {"user_uid": str,
            "user_name": str}}``. No match returns code 2001; missing/invalid
            identifiers return code 1001. These business results use success=True
            and a standard message string; unexpected exceptions use success=False.
        """
        criteria = []
        for name in ("user_uid", "user_name"):
            if name in payload:
                value = payload[name]
                if not isinstance(value, str) or not value.strip():
                    return business_error_result(StoreCode.INVALID_PAYLOAD)
                criteria.append(users.c[name] == value)
        if not criteria:
            return business_error_result(StoreCode.INVALID_PAYLOAD)
        rows = await self._fetch_all(
            select(users.c.user_uid, users.c.user_name).where(*criteria)
        )
        if not rows:
            return business_error_result(StoreCode.USER_NOT_FOUND)
        return {"success": True, "messages": rows[0]}

    @service_handler
    async def list_all_user(self, payload: dict) -> dict:
        """List every registered user in registration order.

        Args:
            payload: Unused dictionary; pass ``{}``.

        Returns:
            ``{"success": True, "code": 0, "messages": [{"user_uid": str,
            "user_name": str}, ...]}``. An empty database returns an empty list.
            Failures use a nonzero code and a standard message string.
        """
        rows = await self._fetch_all(
            select(users.c.user_uid, users.c.user_name).order_by(users.c.id)
        )
        return {"success": True, "messages": rows}

    # --------------------------------------------------
    # Conversations
    # --------------------------------------------------

    @service_handler
    async def list_conversations(self, payload: dict) -> dict:
        """List a user's conversations, pinned first and then most recently active.

        Args:
            payload: Required ``user_uid``. Excludes rows with ``is_deleted = 1``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``,
                ``has_new_message`` and ``is_cron``. Flags are SQLite integers.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``
                and ``has_new_message``. Flags are SQLite integers.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": {"conversation_uid": str}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                conversation matches. Does not update activity timestamps.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
        return {"success": True, "messages": {}}

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
                ``message`` contains flat middleware Message fields: ``role``,
                ordered ``content`` blocks, ``name``, ``protocol``, ``item`` and
                ``origin``; ToolMessage also has ``call_id``. A unique
                ``message_uid`` is required. ``generation_id``, ``node_id`` and
                ``parent_id`` default to empty strings; ``metadata`` and
                ``extensions`` default to objects. JSON values are native data.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"msg_cursor": int, "timestamp": str}}``.
                The timestamp uses a ``T`` separator. Updates ``latest_cursor``
                and ``last_active_at``; ``has_new_message`` is zero for ordinary
                user input and one for assistant and tool messages.
                Enabled embeddings are maintained in the background after commit;
                model inference and indexing failures do not delay this response.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table;
                the insert and conversation update are rolled back together.
            Business rejection: ``{"success": True, "code": int, "messages": str}``,
                with a nonzero code and standard message, returned directly.
        """
        user_uid = payload["user_uid"]
        conversation_uid = payload["conversation_uid"]
        message = payload["message"]
        if not message:
            return business_error_result(StoreCode.INVALID_PAYLOAD)
        for field in ("metadata", "extensions"):
            if message.get(field) is not None and not isinstance(message[field], dict):
                return business_error_result(StoreCode.INVALID_PAYLOAD)
        validate_messages([message_wire_from_record(message)])
        role = message["role"]
        async with self._transaction() as connection:
            # Allocate the cursor with the first write. SQLite serializes this
            # UPDATE across engines, avoiding a read-then-write race.
            result = await database_io(
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
                return business_error_result(StoreCode.CONVERSATION_NOT_FOUND)
            cursor = conversation["latest_cursor"]
            result = await database_io(
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
                        content=encode_json(message["content"]),
                        protocol=message.get("protocol"),
                        item=encode_optional_json(message.get("item")),
                        origin=encode_optional_json(message.get("origin")),
                        call_id=message.get("call_id"),
                        metadata=encode_json(message.get("metadata"), default={}),
                        extensions=encode_json(message.get("extensions"), default={}),
                    )
                    .returning(messages.c.id, messages.c.timestamp)
                )
            )
            inserted = result.mappings().one()
            timestamp = inserted["timestamp"]
        if self._embedding is not None and role in {"user", "assistant"}:
            self._schedule_embedding(inserted["id"])
        return {
            "success": True,
            "messages": normalize_row(
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
                messages. The caller supplies distinct node IDs.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": [{"message_uid": str}, ...]}``.
                Only truthy stored message IDs are returned. Unmatched node IDs
                contribute no rows; empty input is a failure.
                Matching vectors are physically removed from every model table
                in the same transaction, including when embedding is disabled.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
            Business rejection: ``{"success": True, "code": int, "messages": str}``,
                with a nonzero code and standard message, returned directly.
        """
        node_ids = payload["messages"]
        if not node_ids:
            return business_error_result(StoreCode.INVALID_PAYLOAD)
        user_uid = payload["user_uid"]
        conversation_uid = payload["conversation_uid"]
        async with self._transaction() as connection:
            result = await database_io(
                connection.execute(
                    update(messages)
                    .where(
                        messages.c.user_uid == user_uid,
                        messages.c.conversation_uid == conversation_uid,
                        messages.c.node_id.in_(node_ids),
                    )
                    .values(is_deleted=1)
                    .returning(messages.c.id, messages.c.message_uid)
                )
            )
            deleted_rows = result.mappings().all()
            if deleted_rows:
                # Old model tables remain searchable after switching back, so
                # deletions must also remove their entries while embedding is off.
                table_names = await database_io(
                    connection.run_sync(lambda conn: inspect(conn).get_table_names())
                )
                message_ids = [row["id"] for row in deleted_rows]
                for name in table_names:
                    if re.fullmatch(r"messages_embedding_[0-9a-f]{64}", name):
                        table = Table(name, MetaData(), Column("message_id", Integer))
                        await database_io(
                            connection.execute(
                                delete(table).where(table.c.message_id.in_(message_ids))
                            )
                        )
            deleted = [
                {"message_uid": row["message_uid"]}
                for row in deleted_rows
                if row["message_uid"]
            ]
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
            Success: ``{"success": True, "code": 0, "messages": rows, "next_cursor": int}``.
                Rows follow ascending ``msg_cursor`` order and contain
                ``message_uid``, ``msg_cursor``, ``role``, ``name``, ``content``,
                ``protocol``, ``item``, ``origin``, ``call_id``, ``metadata``,
                ``extensions``, ``timestamp``, ``generation_id``, ``node_id``,
                ``parent_id``, ``user_uid``, ``conversation_uid`` and integer
                ``is_deleted``. JSON columns are decoded; internal ``id`` and
                ``conversation_id`` are never returned.
                ``next_cursor`` is the last row's cursor plus one, or the clamped
                input cursor if no rows match.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table, with
                no ``next_cursor`` key.
        """
        after_cursor = max(int(payload.get("cursor", 0)), 0)
        rows = await self._fetch_all(
            select(*MESSAGE_RECORD_COLUMNS)
            .where(
                messages.c.user_uid == payload["user_uid"],
                messages.c.conversation_uid == payload["conversation_uid"],
                messages.c.msg_cursor >= after_cursor,
            )
            .order_by(messages.c.msg_cursor)
            .limit(int(payload.get("limit", 65535))),
            json_fields=("content",),
            row_decoder=decode_message_row,
        )
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
                Optional ``conversation_uid`` scopes the search. Pass the previous
                ``next_cursor`` as ``cursor`` to fetch the next page.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows, "next_cursor": dict | None}``.
                At most 20 nodes, newest matching message per conversation/node,
                ordered by timestamp, msg_cursor and conversation_uid descending.
                Empty node IDs are treated as separate messages. Rows contain the
                complete flat message record plus title and last_active_at.
                The cursor contains timestamp, msg_cursor and conversation_uid;
                fewer than 20 rows returns None. A full final page may be followed
                by an empty page. Internal database IDs are not returned.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        keyword = payload["keyword"]
        if not re.sub(r"[%_\\\s]+", "", keyword):
            return {"success": True, "messages": [], "next_cursor": None}
        keyword = re.sub(r"[_\\\s]+", "%", keyword)
        keyword = re.sub(r"%+", "%", keyword).strip("%")
        return await self._search_messages_page(
            payload, messages.c.content.like(f"%{keyword}%")
        )

    @service_handler
    async def search_messages_vec(self, payload: dict) -> dict:
        """Search message bodies by cosine similarity, then paginate by time.

        Args:
            payload: Required ``user_uid`` and ``keyword`` strings. Optional
                ``conversation_uid`` scopes the search. Pass the previous
                ``next_cursor`` as ``cursor`` (timestamp, msg_cursor and
                conversation_uid). ``min_similarity`` defaults to 0.5 and must
                be between -1 and 1. Disabled embedding uses keyword search.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows,
            "next_cursor": dict | None}``. Uses the node grouping, message
            records and reverse chronological order of ``search_messages``;
            vector results additionally contain ``similarity``. Excludes stale
            vectors before the 20-node page limit. Empty keywords return no
            matches. A None cursor ends pagination.
            Failure: ``{"success": False, "code": int, "messages": str}``,
                using the standard database.utils.errors table.
            Business rejection: ``{"success": True, "code": int, "messages": str}``,
                with a nonzero code and standard message, returned directly.
        """
        if self._embedding is None:
            return await self.search_messages(payload)
        keyword = payload["keyword"].strip()
        if not keyword:
            return {"success": True, "messages": [], "next_cursor": None}
        minimum = float(payload.get("min_similarity", 0.5))
        if not -1 <= minimum <= 1:
            return business_error_result(StoreCode.INVALID_PAYLOAD)
        vector = (await self._embedding.encode([keyword], query=True))[0]
        table = self._embedding.table
        similarity = 1 - func.vec_distance_cosine(table.c.embedding, vector)
        return await self._search_messages_page(
            payload, similarity >= minimum, similarity=similarity
        )

    async def _search_messages_page(
        self,
        payload: dict,
        predicate: ColumnElement[bool],
        *,
        similarity: ColumnElement[float] | None = None,
    ) -> dict:
        source = messages.join(
            conversations, conversations.c.id == messages.c.conversation_id
        )
        extra_columns = []
        if similarity is not None:
            table = self._embedding.table
            source = source.join(table, table.c.message_id == messages.c.id)
            extra_columns.append(similarity.label("similarity"))
        matches = (
            select(
                *MESSAGE_RECORD_COLUMNS,
                conversations.c.title,
                conversations.c.last_active_at,
                *extra_columns,
                func.row_number()
                .over(
                    partition_by=(
                        messages.c.conversation_id,
                        messages.c.node_id,
                        case(
                            (messages.c.node_id == "", messages.c.message_uid),
                            else_="",
                        ),
                    ),
                    order_by=(
                        messages.c.timestamp.desc(),
                        messages.c.msg_cursor.desc(),
                    ),
                )
                .label("node_rank"),
            )
            .select_from(source)
            .where(
                conversations.c.user_uid == payload["user_uid"],
                conversations.c.is_deleted == 0,
                messages.c.is_deleted == 0,
                messages.c.role.in_(["user", "assistant"]),
                predicate,
            )
        )
        if payload.get("conversation_uid") is not None:
            matches = matches.where(
                conversations.c.conversation_uid == payload["conversation_uid"]
            )
        grouped = matches.subquery()
        page = select(*(c for c in grouped.c if c.name != "node_rank")).where(
            grouped.c.node_rank == 1
        )
        if cursor := payload.get("cursor"):
            page = page.where(
                tuple_(
                    grouped.c.timestamp,
                    grouped.c.msg_cursor,
                    grouped.c.conversation_uid,
                )
                < tuple_(
                    cursor["timestamp"].replace("T", " "),
                    int(cursor["msg_cursor"]),
                    cursor["conversation_uid"],
                )
            )
        rows = await self._fetch_all(
            page.order_by(
                grouped.c.timestamp.desc(),
                grouped.c.msg_cursor.desc(),
                grouped.c.conversation_uid.desc(),
            ).limit(20),
            json_fields=("content",),
            row_decoder=decode_message_row,
        )
        return {
            "success": True,
            "messages": rows,
            "next_cursor": {
                key: rows[-1][key]
                for key in ("timestamp", "msg_cursor", "conversation_uid")
            }
            if len(rows) == 20
            else None,
        }

    def _unindexed_messages(self) -> Select:
        table = self._embedding.table
        return select(messages.c.id, messages.c.content).where(
            messages.c.is_deleted == 0,
            messages.c.role.in_(["user", "assistant"]),
            ~select(table.c.message_id)
            .where(table.c.message_id == messages.c.id)
            .exists(),
        )

    async def _embed_message_rows(self, rows: list[dict]) -> int:
        bodies = []
        for row in rows:
            body = "\n".join(
                block["text"] for block in row["content"] if block["type"] == "text"
            )
            if body.strip():
                bodies.append((row["id"], body))
        if not bodies:
            return 0
        vectors = await self._embedding.encode([body for _, body in bodies])
        return await self._execute(
            sqlite_insert(self._embedding.table).on_conflict_do_nothing(
                index_elements=["message_id"]
            ),
            [
                {"message_id": message_id, "embedding": vector}
                for (message_id, _), vector in zip(bodies, vectors, strict=True)
            ],
        )

    async def _index_message(self, message_id: int) -> None:
        rows = await self._fetch_all(
            self._unindexed_messages().where(messages.c.id == message_id),
            json_fields=("content",),
        )
        await self._embed_message_rows(rows)

    @service_handler
    async def sync_messages_embedding(self, payload: dict | None = None) -> dict:
        """Submit a background reconciliation of the current model's index.

        Args:
            payload: Optional unused dictionary; pass ``{}`` through the store
                action. Requires an initialized service with embedding enabled.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"task_id": str}}``.
                Returns immediately. While a sync is active, repeated calls
                return the same task ID without starting or queuing another sync.
                Caller cancellation does not cancel the submitted task. Use
                ``get_messages_embedding_sync_status`` to query its progress.
                After completion or failure, another call starts a new task.
            Disabled embedding: ``{"success": True, "code": 2004,
            "messages": "Message embedding is disabled."}``, returned directly.
            Unexpected exceptions use success=False and a standard error code
                and message. Background failures are recorded in task status.
            Business rejection: ``{"success": True, "code": int, "messages": str}``,
                with a nonzero code and standard message, returned directly.
        """
        if self._embedding is None:
            return business_error_result(StoreCode.EMBEDDING_DISABLED)
        # Admission has no await: calls on the store loop share one active task.
        if self._embedding_sync_task is None or self._embedding_sync_task.done():
            self._embedding_sync_status = {
                "task_id": str(uuid4()),
                "status": "running",
                "inserted": 0,
                "deleted": 0,
                "error": None,
            }
            self._embedding_sync_task = asyncio.create_task(
                self._sync_messages_embedding(self._embedding_sync_status)
            )
        return {
            "success": True,
            "messages": {"task_id": self._embedding_sync_status["task_id"]},
        }

    @service_handler
    async def get_messages_embedding_sync_status(
        self, payload: dict | None = None
    ) -> dict:
        """Get a snapshot of the current model's most recent sync task.

        Args:
            payload: Optional dictionary with ``task_id`` to identify the task.
                Omit it or pass ``{}`` to query the most recent task.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": status}``. Status contains
                ``task_id``, ``status`` (running/completed/failed), ``inserted``
                and ``deleted`` committed record counts, and nullable ``error``.
                Only the most recent task is retained; restarting the service
                clears this in-memory status.
            Missing or mismatched task: ``{"success": True, "code": 2005,
            "messages": "Message embedding sync task not found."}``, returned directly.
            Unexpected exceptions use success=False and a standard error code
                and message.
            Business rejection: ``{"success": True, "code": int, "messages": str}``,
                with a nonzero code and standard message, returned directly.
        """
        status = self._embedding_sync_status
        if status is None:
            return business_error_result(StoreCode.SYNC_TASK_NOT_FOUND)
        if payload and payload.get("task_id", status["task_id"]) != status["task_id"]:
            return business_error_result(StoreCode.SYNC_TASK_NOT_FOUND)
        return {"success": True, "messages": dict(status)}

    async def _sync_messages_embedding(self, status: dict[str, Any]) -> None:
        """Reconcile missing and stale vectors, recording committed progress."""
        try:
            await self._wait_for_embedding_tasks()
            table = self._embedding.table
            status["deleted"] = await self._execute(
                delete(table).where(
                    ~select(messages.c.id)
                    .where(
                        messages.c.id == table.c.message_id, messages.c.is_deleted == 0
                    )
                    .exists()
                )
            )
            last_id = 0
            while rows := await self._fetch_all(
                self._unindexed_messages()
                .where(messages.c.id > last_id)
                .order_by(messages.c.id)
                .limit(64),
                json_fields=("content",),
            ):
                status["inserted"] += await self._embed_message_rows(rows)
                last_id = rows[-1]["id"]
            status["status"] = "completed"
        except Exception as exc:  # noqa: BLE001 - background task failure boundary
            status["status"] = "failed"
            status["error"] = ERROR_MESSAGES[exception_code(exc)]
            logger.exception("Message embedding sync failed: %s", exc)

    # --------------------------------------------------
    # Compressed context
    # --------------------------------------------------

    @service_handler
    async def list_compressed_contexts(self, payload: dict) -> dict:
        """Get only the latest undeleted compressed context for a conversation.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row
                containing ``memory_id``, ``content`` and integer
                ``created_timestamp`` (Unix microseconds).
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        rows = await self._fetch_all(
            select(
                compressed_context.c.memory_id,
                compressed_context.c.content,
                compressed_context.c.created_timestamp,
            )
            .where(
                compressed_context.c.user_uid == payload["user_uid"],
                compressed_context.c.conversation_uid == payload["conversation_uid"],
                compressed_context.c.is_deleted == 0,
            )
            .order_by(compressed_context.c.created_timestamp.desc())
            .limit(1)
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def create_compressed_context(self, payload: dict) -> dict:
        """Insert one compressed context with the current Unix microsecond timestamp.

        Args:
            payload: Required ``memory_id``, ``user_uid``, ``conversation_uid``
                and string ``content``. The memory ID must be unique; user and
                conversation foreign keys must exist.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        await self._execute(
            insert(compressed_context).values(
                memory_id=payload["memory_id"],
                user_uid=payload["user_uid"],
                conversation_uid=payload["conversation_uid"],
                content=payload["content"],
                created_timestamp=int(time.time() * 1_000_000),
            )
        )
        return {"success": True, "messages": {}}

    @service_handler
    async def delete_compressed_contexts(self, payload: dict) -> dict:
        """Soft-delete matching compressed contexts in a user's conversation.

        Args:
            payload: Required ``memory_ids`` list. When nonempty, also requires
                ``user_uid`` and ``conversation_uid``. Only undeleted rows match.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, including
                empty input or no matching rows.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        memory_ids = payload["memory_ids"]
        if memory_ids:
            await self._execute(
                update(compressed_context)
                .where(
                    compressed_context.c.user_uid == payload["user_uid"],
                    compressed_context.c.conversation_uid
                    == payload["conversation_uid"],
                    compressed_context.c.is_deleted == 0,
                    compressed_context.c.memory_id.in_(memory_ids),
                )
                .values(is_deleted=1)
            )
        return {"success": True, "messages": {}}

    # --------------------------------------------------
    # LLM providers
    # --------------------------------------------------

    @service_handler
    async def create_llm_provider(self, payload: dict) -> dict:
        """Insert a shared LLM provider.

        Args:
            payload: Required ``provider_id``, ``provider_name``,
                ``endpoint`` and JSON-serializable ``model_list`` (normally a list).
                ``type`` defaults to ``"openai"`` when absent or falsey and is
                lowercased; the schema permits ``openai`` and ``anthropic``.
                ``description`` defaults to ``None``. The provider ID must be unique.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"provider_id": provider_id}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        provider_id = payload["provider_id"]
        await self._execute(
            insert(llm_provider).values(
                provider_id=provider_id,
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
        """List all undeleted LLM providers, newest creation first.

        Args:
            payload: No required keys; use an empty dictionary.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``provider_id``, ``provider_name``, ``type``, ``endpoint``,
                decoded ``model_list``, nullable ``description`` and string
                ``created_at``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            .where(llm_provider.c.is_deleted == 0)
            .order_by(llm_provider.c.created_at.desc()),
            json_fields=("model_list",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def get_llm_provider(self, payload: dict) -> dict:
        """Get one undeleted LLM provider by ID.

        Args:
            payload: Required ``provider_id``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row
                shaped as in ``list_llm_providers``; ``model_list`` is decoded.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            payload: Required ``provider_id``. Optional
                ``provider_name``, ``type``, ``endpoint``, ``model_list``,
                ``description`` and ``is_deleted``; absent or ``None`` values
                leave fields unchanged. String ``type`` is lowercased. A list
                ``model_list`` is encoded as a native JSON value.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted provider matches. Deleted providers cannot be restored.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        provider_type = payload.get("type")
        if isinstance(provider_type, str):
            provider_type = provider_type.lower()
        model_list = encode_optional_json(payload.get("model_list"))
        await self._execute(
            update(llm_provider)
            .where(
                llm_provider.c.provider_id == payload["provider_id"],
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
        return {"success": True, "messages": {}}

    # --------------------------------------------------
    # MCP servers
    # --------------------------------------------------

    @service_handler
    async def create_mcp_server(self, payload: dict) -> dict:
        """Insert shared MCP server metadata.

        Args:
            payload: Required ``mcp_id``, ``mcp_name``, ``transport``
                and ``endpoint`` (which may be ``None``). Transport must be
                ``stdio``, ``http``, ``streamable_http``, ``websocket`` or ``sse``.
                Optional JSON-serializable ``config`` defaults to an empty object
                when absent; ``description`` defaults to ``None``. IDs are unique.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"mcp_id": mcp_id}}``.
                New servers are enabled with a tool count of zero.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        mcp_id = payload["mcp_id"]
        await self._execute(
            insert(mcp_server).values(
                mcp_id=mcp_id,
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
        """List all undeleted MCP servers, newest creation first.

        Args:
            payload: No required keys; use an empty dictionary.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint``, decoded ``config``, nullable ``description``,
                integer ``enabled`` and ``tool_count``, and string ``created_at``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            .where(mcp_server.c.is_deleted == 0)
            .order_by(mcp_server.c.created_at.desc()),
            json_fields=("config",),
        )
        return {"success": True, "messages": rows}

    @service_handler
    async def list_enabled_mcp_servers(self, payload: dict) -> dict:
        """List all enabled, undeleted MCP servers, oldest creation first.

        Args:
            payload: No required keys; use an empty dictionary.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint`` and decoded ``config``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            payload: Required ``mcp_id``. Optional ``mcp_name``,
                ``transport``, ``endpoint``, ``config``, ``description``, ``enabled``,
                ``tool_count`` and ``is_deleted``; absent or ``None`` values leave
                fields unchanged. Config is encoded as a native JSON value.
                Transport retains its schema constraints.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted server matches. Deleted servers cannot be restored.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
        """
        config = encode_optional_json(payload.get("config"))
        await self._execute(
            update(mcp_server)
            .where(
                mcp_server.c.mcp_id == payload["mcp_id"],
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
        return {"success": True, "messages": {}}

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
            Success: ``{"success": True, "code": 0, "messages": {"task_id": task_id}}``.
                New tasks are enabled by default.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``user_uid``, ``conversation_uid``,
                ``platform``, ``name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, decoded ``extra_config``, nullable ``description``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``conversation_uid``, ``platform``, ``name``,
                ``prompt``, ``execute``, ``exec_time``, ``repeat``, decoded
                ``extra_config``, nullable ``description``, integer ``enabled``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one
                row shaped as in ``list_cron_tasks`` plus ``user_uid``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted task matches. Matching rows update ``updated_at``;
                deleted tasks cannot be restored by this method.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table.
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
        return {"success": True, "messages": {}}
