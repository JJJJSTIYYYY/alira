"""Run Uvicorn as a managed asynchronous service."""

import asyncio
from contextlib import contextmanager
from urllib.parse import urlparse

import uvicorn

from alira.modules.web.application import create_app
from alira.shared.application.config import BASE_URL
from alira.utils.logger import get_uvicorn_log_config


class ModuleServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Process signals belong to the kernel entry point, not an HTTP module.
        yield


class WebRuntime:
    def __init__(self):
        parsed = urlparse(BASE_URL)
        self.app = create_app()
        self.server = ModuleServer(uvicorn.Config(
            self.app, host=parsed.hostname or "0.0.0.0",
            port=parsed.port if parsed.port is not None else 2712,
            log_config=get_uvicorn_log_config(),
        ))
        self.task: asyncio.Task[None] | None = None
        self._ready = False

    async def _serve(self) -> None:
        try:
            await self.server.serve()
        except SystemExit as exc:
            raise RuntimeError(f"Web server startup failed: {exc.code}") from exc

    async def start(self) -> None:
        self.task = asyncio.create_task(self._serve(), name="alira:web")
        while not self.server.started:
            if self.task.done():
                await self.task
                raise RuntimeError("Web server stopped before becoming ready")
            await asyncio.sleep(0.01)
        self._ready = True

    async def stop(self) -> None:
        if self.task is not None:
            self.server.should_exit = True
            if self._ready:
                await self.task
            else:
                # startup() already reports a serving failure; still reap its task.
                await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
