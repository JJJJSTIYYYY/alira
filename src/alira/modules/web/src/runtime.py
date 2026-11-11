"""Run Uvicorn as a managed asynchronous service."""

import asyncio
from contextlib import contextmanager
from urllib.parse import urlparse

import uvicorn
from starlette.types import Receive, Scope, Send

from alira.modules.web.config import BASE_URL, SHUTDOWN_TIMEOUT, STARTUP_TIMEOUT
from alira.modules.web.src.application import create_app
from alira.modules.web.src.logging import get_uvicorn_log_config


class ModuleServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config):
        super().__init__(config)
        self.ready = asyncio.Event()
        self.servers: list[asyncio.Server] = []

    @contextmanager
    def capture_signals(self):
        # Process signals belong to the kernel entry point, not an HTTP module.
        yield

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.ready.set()


class WebRuntime:
    def __init__(self):
        if STARTUP_TIMEOUT <= 0 or SHUTDOWN_TIMEOUT <= 0:
            raise ValueError("Web startup_timeout and shutdown_timeout must be positive")
        parsed = urlparse(BASE_URL)
        self.app = create_app()
        self._lifespan_task: asyncio.Task | None = None
        self.server = ModuleServer(uvicorn.Config(
            self._application, host=parsed.hostname or "0.0.0.0",
            port=parsed.port if parsed.port is not None else 2712,
            interface="asgi3", lifespan="on",
            log_config=get_uvicorn_log_config(),
            timeout_graceful_shutdown=SHUTDOWN_TIMEOUT,
        ))
        self.task: asyncio.Task[None] | None = None
        self._ready = False

    async def _application(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Uvicorn creates its lifespan task independently of serve(). Keep its
        # handle so a cancelled or timed-out startup cannot leave it running.
        if scope["type"] == "lifespan":
            self._lifespan_task = asyncio.current_task()
            try:
                await self.app(scope, receive, send)
            finally:
                self._lifespan_task = None
        else:
            await self.app(scope, receive, send)

    async def _serve(self) -> None:
        try:
            await self.server.serve()
        except SystemExit as exc:
            raise RuntimeError(f"Web server startup failed: {exc.code}") from exc

    async def start(self) -> None:
        self.task = asyncio.create_task(self._serve(), name="alira:web")
        ready = asyncio.create_task(self.server.ready.wait(), name="alira:web:ready")
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                done, _ = await asyncio.wait(
                    (self.task, ready), return_when=asyncio.FIRST_COMPLETED,
                )
                if self.task in done:
                    await self.task
                    raise RuntimeError("Web server stopped before becoming ready")
            self._ready = True
        except TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                f"Web server startup timed out after {STARTUP_TIMEOUT:g} seconds"
            ) from exc
        except BaseException:
            await self.stop()
            raise
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def _cancel_tasks(self) -> None:
        self._ready = False
        self.server.force_exit = True
        tasks = set(self.server.server_state.tasks)
        if self.task is not None:
            tasks.add(self.task)
        if self._lifespan_task is not None:
            tasks.add(self._lifespan_task)
        for task in tasks:
            task.cancel()
        for connection in list(self.server.server_state.connections):
            connection.shutdown()
        for server in self.server.servers:
            server.close()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT)
            await asyncio.gather(*done, return_exceptions=True)
            if pending:
                # Cancellation can enter application teardown. Interrupt that
                # teardown too if it exceeds the cleanup deadline.
                for task in pending:
                    task.cancel()
                done, pending = await asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT)
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                raise RuntimeError("Web server tasks did not stop after cancellation")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.server.should_exit = True
        try:
            if self._ready:
                try:
                    async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                        await asyncio.shield(self.task)
                except TimeoutError as exc:
                    await self._cancel_tasks()
                    raise RuntimeError(
                        f"Web server shutdown timed out after {SHUTDOWN_TIMEOUT:g} seconds"
                    ) from exc
                except BaseException:
                    await self._cancel_tasks()
                    raise
            else:
                # startup() reports serving failures; still reap partial startup.
                await self._cancel_tasks()
        finally:
            if (
                self.task.done()
                and self._lifespan_task is None
                and all(task.done() for task in self.server.server_state.tasks)
            ):
                self.task = None
                self._ready = False
