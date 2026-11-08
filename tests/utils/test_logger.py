"""Lossless running buffers, lifecycle barriers, failure reporting and rotation."""

import asyncio
import importlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

import pytest

from alira.utils.logger import Logger, LogWriter

logging = importlib.import_module("alira.utils.logger")


def lines(root, name="test"):
    paths = sorted((root / name).glob("*.log"), key=lambda p: p.stat().st_mtime_ns)
    return [line for path in paths for line in path.read_text().splitlines()]


@pytest.fixture
def writer(tmp_path, monkeypatch):
    instance = LogWriter(str(tmp_path), 100, 1024 * 1024, interval=0.01)
    monkeypatch.setattr(Logger, "_writer", instance)
    monkeypatch.setattr(logging, "ALIRA_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(logging, "DEBUG_LEVEL", "DEBUG")
    yield instance
    try:
        instance.stop()
    except RuntimeError:
        pass
    assert instance._thread is None


async def test_console_format_traceback_separator_and_plain_files(
    writer, tmp_path, capsys
):
    log = Logger("test", show_time=False)
    await log.start()
    log.info("value=%s", 2, details={"key": 1})
    log.info(123, "extra")
    log.separator(length=3)
    try:
        raise ValueError("traceback-sentinel")
    except ValueError:
        log.exception("failed")
    log.exception("outside handler")
    await log.flush()
    out, err = capsys.readouterr()
    assert (
        "[test_logger GLOBAL test_console_format_traceback_separator_and_plain_files]"
        in out
    )
    assert "value=2" in out and "details={'key': 1}" in out
    assert "123 extra" in out and "---" in out
    assert "ValueError: traceback-sentinel" in err
    content = "\n".join(lines(tmp_path))
    assert "Traceback (most recent call last)" in content
    assert "[EXCEPTION]" in content
    assert "\033" not in content and "NoneType: None" not in content


async def test_filtered_message_does_not_format_or_enqueue(writer, monkeypatch):
    monkeypatch.setattr(logging, "DEBUG_LEVEL", "ERROR")

    class Unprintable:
        def __str__(self):
            raise AssertionError("filtered log formatted")

    Logger().debug(Unprintable())
    assert writer._submitted == 0


@pytest.mark.parametrize("capacity", [1, 2, 100])
def test_burst_and_multiple_producers_keep_every_record(tmp_path, capacity):
    writer = LogWriter(str(tmp_path), capacity, 10**7)
    writer.start()
    try:

        def produce(producer):
            for i in range(150):
                writer.enqueue("test", f"{producer}:{i}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(produce, range(4)))
        writer.flush()
        result = lines(tmp_path)
        assert len(result) == len(set(result)) == 600
        for producer in range(4):
            assert [s for s in result if s.startswith(f"{producer}:")] == [
                f"{producer}:{i}" for i in range(150)
            ]
        assert writer.dropped_records == 0
    finally:
        writer.stop()


def test_low_volume_flushes_without_explicit_barrier(writer, tmp_path, monkeypatch):
    written = Event()
    original = writer.write_batch

    def capture(batch):
        original(batch)
        written.set()

    monkeypatch.setattr(writer, "write_batch", capture)
    writer.start()
    writer.enqueue("test", "one")
    assert written.wait(1)
    assert lines(tmp_path) == ["one"]


def test_prestart_eviction_is_counted_and_reported(tmp_path, capsys):
    writer = LogWriter(str(tmp_path), 2, 1000)
    for i in range(4):
        writer.enqueue("test", str(i))
    writer.stop()  # Also drains records even without an earlier start().
    assert lines(tmp_path) == ["2", "3"]
    assert writer.dropped_records == 2
    assert "2 pre-start records evicted" in capsys.readouterr().err


def test_utf8_rotation_restarts_existing_file_and_oversized_record(tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    basename = f"{today}_{LogWriter.STARTUP_SUFFIX}"
    directory = tmp_path / "test"
    directory.mkdir()
    (directory / f"{basename}.log").write_bytes(b"123456\n")
    writer = LogWriter(str(tmp_path), 100, 10)
    for message in ["中文", "中文", "a" * 11, "z"]:
        writer.enqueue("test", message)
    writer.stop()
    assert (directory / f"{basename}.log").read_bytes() == b"123456\n"
    assert (directory / f"{basename}_1.log").read_text() == "中文\n"
    assert (directory / f"{basename}_2.log").read_text() == "中文\n"
    assert (directory / f"{basename}_3.log").read_text() == "a" * 11 + "\n"
    assert (directory / f"{basename}_4.log").read_text() == "z\n"
    writer.enqueue("test", "next")
    writer.stop()
    assert (directory / f"{basename}_4.log").read_text() == "z\nnext\n"


def test_file_failure_is_visible_and_other_names_still_write(writer, tmp_path, capsys):
    (tmp_path / "broken").write_text("not a directory")
    writer.enqueue("broken", "failed")
    writer.enqueue("test", "kept")
    with pytest.raises(RuntimeError, match="file output failed"):
        writer.flush()
    assert lines(tmp_path) == ["kept"]
    writer.stop()  # The failure was already surfaced by flush().
    assert writer._thread is None
    assert "Unable to write log batch" in capsys.readouterr().err
    writer.start()  # A new run can recover after a surfaced error.
    writer.enqueue("test", "recovered")
    writer.stop()
    assert lines(tmp_path) == ["kept", "recovered"]


async def test_cancelled_stop_finishes_io_before_restart(writer, tmp_path, monkeypatch):
    entered, release = Event(), Event()
    original = writer.write_batch

    def slow_write(batch):
        entered.set()
        assert release.wait(3)
        original(batch)

    monkeypatch.setattr(writer, "write_batch", slow_write)
    await Logger.start()
    writer.enqueue("test", "before-stop")
    stopping = asyncio.create_task(Logger.stop())
    assert await asyncio.to_thread(entered.wait, 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    restarting = asyncio.create_task(Logger.start())
    await asyncio.sleep(0.02)
    assert not restarting.done()
    release.set()
    await asyncio.wait_for(restarting, 1)
    writer.enqueue("test", "after-restart")
    await Logger.stop()
    assert lines(tmp_path) == ["before-stop", "after-restart"]


async def test_cross_loop_lifecycle_and_concurrent_flush(writer, tmp_path):
    def cycle():
        async def run():
            await Logger.start()
            writer.enqueue("test", "cross-loop")
            await asyncio.gather(*(Logger.flush() for _ in range(4)))
            await Logger.stop()

        asyncio.run(run())

    await asyncio.to_thread(cycle)
    await Logger.start()
    writer.enqueue("test", "main-loop")
    await asyncio.gather(Logger.flush(), Logger.stop(), Logger.stop())
    assert lines(tmp_path) == ["cross-loop", "main-loop"]


async def test_explicit_file_write_preserves_text_and_json(writer, tmp_path):
    log = Logger()
    await log.write_log("logs", "text", "hello\nworld")
    await log.write_log("logs", "json", {"中文": True})
    assert (tmp_path / "logs/text.log").read_text() == "hello\nworld\n"
    assert json.loads((tmp_path / "logs/json.jsonl").read_text()) == {"中文": True}
    with pytest.raises(ValueError, match="inside ALIRA_BASE_DIR"):
        await log.write_log("..", "outside", "blocked")


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "/tmp/escape", "a\\b"])
def test_invalid_logger_directory(name):
    with pytest.raises(ValueError):
        Logger(name)


@pytest.mark.parametrize("capacity,size", [(0, 100), (1, 0), (-1, 100)])
def test_invalid_writer_limits(tmp_path, capacity, size):
    with pytest.raises(ValueError):
        LogWriter(str(tmp_path), capacity, size)


def test_timestamps_follow_second_boundaries_and_clock_rollback(writer, monkeypatch):
    log = Logger("test")
    monkeypatch.setattr(Logger, "_time_cache", (0, ""))
    for second in [1800000000, 1800000000, 1800000001, 1799999999]:
        monkeypatch.setattr(logging.time, "time", lambda second=second: second)
        text = log._get_formatted_message("info", "value")
        assert text.startswith(
            datetime.fromtimestamp(second).strftime("%Y-%m-%d %H:%M:%S")
        )


def test_date_rollover_resets_rotation_indices(tmp_path, monkeypatch):
    writer = LogWriter(str(tmp_path), 100, 2)
    first = datetime(2026, 11, 1, 23, 59, 59).timestamp()
    monkeypatch.setattr(logging.time, "time", lambda: first)
    writer.write_batch([("test", "a"), ("test", "b")])
    monkeypatch.setattr(logging.time, "time", lambda: first + 1)
    writer.write_batch([("test", "c")])
    assert (
        tmp_path / f"test/2026-11-01_{LogWriter.STARTUP_SUFFIX}_1.log"
    ).read_text() == "b\n"
    assert (
        tmp_path / f"test/2026-11-02_{LogWriter.STARTUP_SUFFIX}.log"
    ).read_text() == "c\n"


async def test_stop_failure_can_be_cleaned_up_and_restarted(
    writer, tmp_path, monkeypatch
):
    original = writer.write_batch

    def fail(batch):
        raise OSError("write failed")

    monkeypatch.setattr(writer, "write_batch", fail)
    writer.enqueue("test", "lost-to-io-error")
    with pytest.raises(RuntimeError):
        await Logger.stop()
    await Logger.stop()  # Cleanup remains retryable after reporting the error.
    monkeypatch.setattr(writer, "write_batch", original)
    await Logger.start()
    writer.enqueue("test", "recovered")
    await Logger.stop()
    assert lines(tmp_path) == ["recovered"]


def test_shutdown_drains_accepted_records_and_releases_blocked_producer(tmp_path):
    writer = LogWriter(str(tmp_path), 1, 1000)
    entered, release, producer_done = Event(), Event(), Event()
    original = writer.write_batch

    def blocked_write(batch):
        entered.set()
        assert release.wait(2)
        original(batch)

    writer.write_batch = blocked_write
    writer.start()
    writer.enqueue("test", "active")
    assert entered.wait(1)
    writer.enqueue("test", "pending")

    def blocked_producer():
        writer.enqueue("test", "after-cutoff")
        producer_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(blocked_producer)
        try:
            assert not producer_done.wait(0.02)
            stopping = pool.submit(writer.stop)
            assert producer_done.wait(1)
        finally:
            release.set()
        producer.result(timeout=1)
        stopping.result(timeout=1)
    assert writer._thread is None
    assert lines(tmp_path) == ["active", "pending"]


async def test_server_joins_writer_even_if_service_shutdown_fails(
    writer, monkeypatch, core_lifecycle
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    server = importlib.import_module("alira.server")
    monkeypatch.setattr(server, "auto_load_router", lambda app: None)
    monkeypatch.setattr(server, "auto_init", core_lifecycle)
    core_lifecycle.register(
        SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(side_effect=[ValueError("cleanup failed"), None]),
        )
    )
    app = server.create_app()
    with pytest.raises(ExceptionGroup, match="Failed to stop services") as caught:
        async with app.router.lifespan_context(app):
            assert writer._thread is not None
    assert str(caught.value.exceptions[0]) == "cleanup failed"
    assert writer._thread is None


def test_closed_stderr_does_not_kill_file_worker_or_hang_barriers(
    writer, tmp_path, monkeypatch
):
    class ClosedStream:
        def write(self, value):
            raise ValueError("closed stream")

    monkeypatch.setattr(logging.sys, "stderr", ClosedStream())
    (tmp_path / "broken").write_text("not a directory")
    writer.enqueue("broken", "failed")
    with pytest.raises(RuntimeError, match="file output failed"):
        writer.flush()
    writer.enqueue("test", "still alive")
    writer.stop()
    assert lines(tmp_path) == ["still alive"]


def test_process_starts_get_separate_files_and_loggers_share_suffix(tmp_path):
    legacy = tmp_path / "first" / f"{datetime.now():%Y-%m-%d}.log"
    legacy.parent.mkdir()
    legacy.write_text("previous logs\n")
    script = """
import sys
from importlib import import_module
from alira.utils.logger import Logger, LogWriter

module = import_module('alira.utils.logger')
module.DEBUG_LEVEL = 'DEBUG'
Logger._writer = LogWriter(sys.argv[1], 100, 1024 * 1024)
with Logger.file_logging():
    Logger('first').info(sys.argv[2])
    Logger('second').info(sys.argv[2])
print(LogWriter.STARTUP_SUFFIX)
"""
    suffixes = []
    for marker in ("first-start", "second-start"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), marker],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        suffix = result.stdout.splitlines()[-1]
        assert suffix.isdecimal() and len(suffix) == 9
        suffixes.append(suffix)
        for name in ("first", "second"):
            paths = list((tmp_path / name).glob(f"*_{suffix}.log"))
            assert len(paths) == 1
            assert marker in paths[0].read_text()
            other_marker = "second-start" if marker == "first-start" else "first-start"
            assert other_marker not in paths[0].read_text()
    assert suffixes[0] != suffixes[1]
    assert legacy.read_text() == "previous logs\n"


def test_custom_log_level_registration_controls_output(writer, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logging, "LOG_LEVELS", dict(logging.LOG_LEVELS))
    monkeypatch.setattr(logging, "DEBUG_LEVEL", "INFO")
    log = Logger("test", show_time=False)

    logging.register_log_level("NOTICE", 1)
    with pytest.raises(ValueError, match="has been registered"):
        logging.register_log_level("NOTICE", 2)
    log.custom("visible", level="NOTICE", color="purple")
    logging.register_log_level("NOTICE", 0, exist_ok=True)
    log.custom("filtered", level="NOTICE")
    logging.unregister_log_level("NOTICE")
    log.custom("unregistered", level="NOTICE")
    logging.unregister_log_level("NOTICE")
    with pytest.raises(ValueError, match="has not been registered"):
        logging.unregister_log_level("NOTICE", missing_ok=False)
    writer.flush()

    out, err = capsys.readouterr()
    assert "\033[95m" in out and "[NOTICE]" in out and "visible" in out
    assert "filtered" not in out and "unregistered" not in out and not err
    assert len(lines(tmp_path)) == 1 and "visible" in lines(tmp_path)[0]


@pytest.mark.parametrize("method", ["info", "warning", "error", "exception", "success", "debug"])
@pytest.mark.parametrize("enabled", [True, False])
def test_public_log_methods_apply_threshold_before_output(
    writer, tmp_path, monkeypatch, capsys, method, enabled
):
    monkeypatch.setitem(logging.LOG_LEVELS, "OFF", 100)
    monkeypatch.setattr(logging, "DEBUG_LEVEL", "DEBUG" if enabled else "OFF")
    getattr(Logger("test", show_time=False), method)("level-sentinel")
    writer.flush()

    out, err = capsys.readouterr()
    if enabled:
        target = err if method in {"error", "exception"} else out
        other = out if method in {"error", "exception"} else err
        assert "level-sentinel" in target and not other
        assert len(lines(tmp_path)) == 1
        assert "level-sentinel" in lines(tmp_path)[0]
    else:
        assert not out and not err
        assert lines(tmp_path) == []


def test_caller_class_and_keyword_formatting_with_hidden_level(writer, tmp_path, capsys):
    log = Logger("test", show_time=False, show_level=False)

    class Caller:
        def emit(self):
            log.info("message", plain="word", spaced="two words", count=2, items=[1])

        @classmethod
        def emit_class(cls):
            log.info("classmethod")

    Caller().emit()
    Caller.emit_class()
    writer.flush()

    out, err = capsys.readouterr()
    assert "[test_logger Caller emit]: message" in out
    assert 'plain=word | spaced="two words" | count=2 | items=[1]' in out
    assert "[test_logger Caller emit_class]: classmethod" in out
    assert "[INFO]" not in out and not err
    assert len(lines(tmp_path)) == 2


def test_trace_flag_filters_without_formatting_and_supplies_default_message(
    writer, tmp_path, monkeypatch, capsys
):
    class Unprintable:
        def __str__(self):
            raise AssertionError("disabled trace formatted")

    log = Logger("test", show_time=False)
    monkeypatch.setattr(logging, "TRACE", False)
    log.trace(Unprintable())
    assert writer._submitted == 0
    monkeypatch.setattr(logging, "TRACE", True)
    log.trace()
    log.trace("explicit-trace")
    writer.flush()

    out, err = capsys.readouterr()
    assert "Enter" in out and "explicit-trace" in out and not err
    assert len(lines(tmp_path)) == 2


async def test_explicit_file_write_uses_defaults_for_blank_names(writer, tmp_path, monkeypatch):
    monkeypatch.setattr(logging.time, "time", lambda: 1800000000.0)
    await Logger().write_log(" ", "", {"中文": False})
    path = tmp_path / "logs/1800000000.0.jsonl"
    assert json.loads(path.read_text()) == {"中文": False}
    assert "中文" in path.read_text()


def test_failed_worker_start_is_retryable_without_losing_pending_records(
    writer, tmp_path, monkeypatch
):
    original_start = logging.Thread.start

    def fail(thread):
        raise RuntimeError("thread start failed")

    writer.enqueue("test", "pending")
    with monkeypatch.context() as patch:
        patch.setattr(logging.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="thread start failed"):
            writer.start()
    assert writer._thread is None
    assert logging.Thread.start is original_start
    writer.start()
    writer.stop()
    assert lines(tmp_path) == ["pending"]
