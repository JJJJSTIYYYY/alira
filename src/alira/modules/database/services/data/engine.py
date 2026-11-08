"""SQLite engine configuration, transactions, and cancellation-safe I/O."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from sqlite3 import SQLITE_BUSY, SQLITE_LOCKED

from sqlalchemy import event
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from alira.modules.database.services.data.schema import metadata


async def database_io[T](operation: Awaitable[T]) -> T:
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
async def engine_transaction(
    engine: AsyncEngine, *, immediate: bool = False
) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        if immediate:
            await connection.execution_options(sqlite_begin_immediate=True)
        try:
            await database_io(connection.begin())
            yield connection
        except BaseException:
            await database_io(connection.rollback())
            raise
        else:
            await database_io(connection.commit())


def create_engine(db_path: str) -> AsyncEngine:
    """Configure SQLite connections and explicit SQLAlchemy transactions."""
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=db_path),
        hide_parameters=True,
        connect_args={"timeout": 30},
        **({"pool_size": 1, "max_overflow": 0} if db_path != ":memory:" else {}),
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

    return engine


async def initialize_schema(engine: AsyncEngine) -> None:
    """Create current tables, retrying concurrent SQLite initialization."""
    for attempt in range(4):
        try:
            async with engine_transaction(engine, immediate=True) as connection:
                await database_io(connection.run_sync(metadata.create_all))
            break
        except OperationalError as exc:
            # Concurrent first connections can race while enabling WAL.
            # busy_timeout does not wait for every journal-mode change.
            code = getattr(exc.orig, "sqlite_errorcode", 0)
            if code & 0xFF not in {SQLITE_BUSY, SQLITE_LOCKED} or attempt == 3:
                raise
            await asyncio.sleep(0.05 * (2**attempt))
