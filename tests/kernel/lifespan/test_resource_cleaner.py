"""Isolated cleaner instances preserve the process singleton and live resources."""

from alira.kernel.lifespan.resource_cleaner import ResourceCleaner, resource_cleaner


def test_isolated_cleaners_do_not_replace_or_share_process_state(resource_cleaner_factory):
    first = resource_cleaner_factory()
    second = resource_cleaner_factory()
    first.register(lambda: 1)
    assert first is not second
    assert first is not resource_cleaner
    assert second._cleaners == []
    assert first._cleaners is not resource_cleaner._cleaners
    assert ResourceCleaner() is resource_cleaner


async def test_creating_isolated_cleaner_does_not_discard_running_work(resource_cleaner_factory):
    running = resource_cleaner_factory()
    await running.start()
    task = running._task
    try:
        isolated = resource_cleaner_factory()
        assert running._task is task
        assert not task.done()
        assert running._running
        assert isolated._task is None
        assert not isolated._running
        await isolated.stop()
        assert not task.done()
    finally:
        await running.stop()
    assert task.done()
    assert running._task is None
    assert ResourceCleaner() is resource_cleaner
