"""FastAPI application and router discovery, owned by the web module."""

import importlib
import pkgutil
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

import alira.modules.web.src.router as routers_pkg
from alira._version import __version__
from alira.shared.logger import logger


def auto_load_router(app: FastAPI) -> None:
    for _, module_name, _ in pkgutil.iter_modules(routers_pkg.__path__):
        full_name = f"{routers_pkg.__name__}.{module_name}"
        module = importlib.import_module(full_name)
        for attr, obj in vars(module).items():
            if isinstance(obj, APIRouter):
                app.include_router(obj)
                logger.success(f"Router registered: {full_name}.{attr}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    auto_load_router(app)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Alira Agent", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health_check():
        return JSONResponse({"status": "ok", "service": "alira"})

    return app
