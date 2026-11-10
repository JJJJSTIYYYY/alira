"""Uvicorn logging configuration for the web module."""

from typing import Any

from alira.shared.logger import AliraLogHandler


def get_uvicorn_log_config() -> dict[str, Any]:
    """Replace Uvicorn handlers; application settings perform level filtering."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"alira": {"()": AliraLogHandler}},
        "loggers": {
            "uvicorn": {"handlers": ["alira"], "level": "DEBUG", "propagate": False},
            **{
                name: {"handlers": [], "level": "NOTSET", "propagate": True}
                for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")
            },
        },
    }
