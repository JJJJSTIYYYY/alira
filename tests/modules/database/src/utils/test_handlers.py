"""Shared operation results, sanitized failures, and cancellation."""

import asyncio
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from alira.modules.database.src.utils import handlers as service_utils
from alira.modules.database.src.utils.errors import StoreCode, StoreError, failure_result
from alira.modules.database.src.utils.handlers import service_handler


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (StoreError(StoreCode.USER_NOT_FOUND), StoreCode.USER_NOT_FOUND),
        (KeyError("private key"), StoreCode.INVALID_PAYLOAD),
        (ValueError("private payload"), StoreCode.INVALID_PAYLOAD),
        (TypeError("private type"), StoreCode.INVALID_PAYLOAD),
        (
            IntegrityError("private SQL", {}, Exception("private value")),
            StoreCode.CONSTRAINT_VIOLATION,
        ),
        (
            OperationalError("private SQL", {}, Exception("private value")),
            StoreCode.DATABASE_ERROR,
        ),
        (OSError("private path"), StoreCode.FILE_ERROR),
        (RuntimeError("private internals"), StoreCode.INTERNAL_ERROR),
    ],
)
async def test_failures_are_coded_and_sanitized_without_business_tracebacks(
    monkeypatch, error, code
):
    exception = Mock()
    monkeypatch.setattr(service_utils.logger, "exception", exception)

    @service_handler
    async def failing():
        raise error

    assert await failing() == failure_result(code)
    assert exception.call_count == int(code == StoreCode.INTERNAL_ERROR)


async def test_success_adds_code_and_cancellation_propagates():
    @service_handler
    async def successful():
        return {"success": True, "messages": {"value": 1}}

    @service_handler
    async def cancelled():
        raise asyncio.CancelledError

    assert successful.__name__ == "successful"
    assert await successful() == {"success": True, "code": 0, "messages": {"value": 1}}
    with pytest.raises(asyncio.CancelledError):
        await cancelled()


async def test_nested_handlers_preserve_nonzero_business_code(monkeypatch):
    exception = Mock()
    monkeypatch.setattr(service_utils.logger, "exception", exception)
    expected = {"success": True, "code": 2001, "messages": "User not found."}

    @service_handler
    async def business_operation():
        return expected

    @service_handler
    async def orchestration():
        return await business_operation()

    assert await orchestration() == expected
    assert expected["code"] == 2001
    exception.assert_not_called()
