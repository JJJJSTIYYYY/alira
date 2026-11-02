"""Generation context restoration and isolation between asynchronous tasks."""

import asyncio

import pytest

from alira.common.type.generation import (
    Generation,
    generation_context,
    get_generation,
)


def generation(name):
    return Generation(
        generation_id=name,
        identity={"id": "user", "platform": "test", "conversation_uid": name},
    )


def test_nested_generation_context_restores_after_exception():
    outer, inner = generation("outer"), generation("inner")
    with pytest.raises(RuntimeError, match="get_generation"):
        get_generation()

    with generation_context(outer):
        assert get_generation() is outer
        with pytest.raises(ValueError, match="nested failure"):
            with generation_context(inner):
                assert get_generation() is inner
                raise ValueError("nested failure")
        assert get_generation() is outer

    with pytest.raises(RuntimeError, match="get_generation"):
        get_generation()


async def test_generation_context_is_isolated_between_tasks():
    parent, first, second = [generation(name) for name in ("parent", "first", "second")]

    async def worker(item):
        assert get_generation() is parent
        with generation_context(item):
            await asyncio.sleep(0)
            assert get_generation() is item
        assert get_generation() is parent

    with generation_context(parent):
        await asyncio.gather(worker(first), worker(second))
        assert get_generation() is parent
