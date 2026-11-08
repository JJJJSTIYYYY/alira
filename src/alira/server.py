"""HTTP application and Uvicorn entry point with shared application logging."""

from alira.shared.application.version import print_logo

print_logo()

import importlib
import pkgutil
from contextlib import asynccontextmanager
from urllib.parse import ParseResult, urlparse

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

import alira.router as routers_pkg
from alira._version import __version__
from alira.utils.logger import get_uvicorn_log_config, logger
from alira.shared.application.config import BASE_URL
from alira.lifespan.auto_init import auto_init


def auto_load_router(app: FastAPI):
    pkg_path = routers_pkg.__path__

    for _, module_name, _ in pkgutil.iter_modules(pkg_path):
        full_name = f"alira.router.{module_name}"
        logger.success(f"Load router module: {full_name}")

        module = importlib.import_module(full_name)

        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, APIRouter):
                app.include_router(obj)
                logger.success(f"✔ Router register: {full_name}.{attr}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        auto_load_router(app)
        await auto_init.start()
        yield
    finally:
        await auto_init.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Alira Agent", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health_check():
        return JSONResponse({"status": "ok", "service": "alira"})

    return app


def run_server() -> None:
    """Keep file output alive until Uvicorn has emitted its final messages."""
    parsed: ParseResult = urlparse(BASE_URL)
    host = parsed.hostname or "0.0.0.0"
    port = parsed.port or 2712
    with logger.file_logging():
        uvicorn.run(
            create_app(),
            host=host,
            port=port,
            reload=False,
            log_config=get_uvicorn_log_config(),
        )


if __name__ == "__main__":
    run_server()
