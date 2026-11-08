"""Shared operation failure responses."""

from unittest.mock import Mock

import pytest

from alira.modules.database.utils import handlers as service_utils
from alira.modules.database.utils.handlers import service_handler


@pytest.mark.asyncio
async def test_service_handler_logs_and_normalizes_failures(monkeypatch):
    exception = Mock()
    monkeypatch.setattr(service_utils.logger, "exception", exception)

    @service_handler
    async def successful(value):
        return {"success": True, "messages": value}

    @service_handler
    async def failing():
        raise ValueError("service unavailable")

    @service_handler(
        failure_factory=lambda exc: {
            "success": False,
            "messages": {"error": type(exc).__name__},
        }
    )
    async def custom_failure():
        raise KeyError("missing identity")

    assert successful.__name__ == "successful"
    assert await successful("stored") == {
        "success": True,
        "messages": "stored",
    }
    assert await failing() == {
        "success": False,
        "messages": "fail: service unavailable",
    }
    assert await custom_failure() == {
        "success": False,
        "messages": {"error": "KeyError"},
    }
    assert exception.call_count == 2
