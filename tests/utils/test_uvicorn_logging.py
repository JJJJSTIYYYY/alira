"""Uvicorn records retain their source and reach the shared console/file sink once."""

import importlib
import logging
import sys
from datetime import datetime

import pytest
import uvicorn
from uvicorn.middleware.message_logger import MessageLoggerMiddleware

from alira.utils.logger import (
    AliraLogHandler,
    Logger,
    LogWriter,
    get_uvicorn_log_config,
)

application_logging = importlib.import_module("alira.utils.logger")


@pytest.fixture
def output(tmp_path, monkeypatch):
    writer = LogWriter(str(tmp_path), 2, 1024 * 1024, interval=0.01)
    monkeypatch.setattr(Logger, "_writer", writer)
    monkeypatch.setattr(application_logging, "DEBUG_LEVEL", "DEBUG")
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    previous = []
    for name in names:
        source = logging.getLogger(name)
        previous.append(
            (
                source,
                source.handlers[:],
                source.level,
                source.propagate,
                source.disabled,
            )
        )
    writer.start()
    try:
        yield writer
    finally:
        writer.stop()
        for source, handlers, level, propagate, disabled in previous:
            source.handlers = handlers
            source.setLevel(level)
            source.propagate = propagate
            source.disabled = disabled


def file_text(root):
    return "".join(path.read_text() for path in (root / "ALIRA").glob("*.log"))


def test_record_preserves_original_time_source_and_detached_exception(
    output, tmp_path, capsys
):
    try:
        raise ValueError("detached-exception")
    except ValueError:
        exception = sys.exc_info()
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        "/source/server.py",
        42,
        "failed %(operation)s",
        ({"operation": "request"},),
        exception,
        func="serve",
    )
    record.created = 1800000000.0
    record.stack_info = "Stack (most recent call last): stack-sentinel"
    original = record.__dict__.copy()
    AliraLogHandler().handle(record)
    output.flush()
    out, err = capsys.readouterr()
    assert not out
    assert err.startswith(
        "\033[91m"
        + datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
    )
    assert "[ALIRA] [ERROR] [uvicorn.error server serve:42]: failed request" in err
    assert err.count("ValueError: detached-exception") == 1
    assert "stack-sentinel" in err
    text = file_text(tmp_path)
    assert text.count("ValueError: detached-exception") == 1
    assert "\033" not in text and "NoneType: None" not in text
    assert record.__dict__ == original


@pytest.mark.parametrize(
    "minimum,expected",
    [
        ("DEBUG", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
        ("INFO", ["INFO", "WARNING", "ERROR", "CRITICAL"]),
        ("WARN", ["WARNING", "ERROR", "CRITICAL"]),
        ("ERROR", ["ERROR", "CRITICAL"]),
    ],
)
def test_standard_levels_use_application_filter_and_streams(
    output, tmp_path, monkeypatch, capsys, minimum, expected
):
    monkeypatch.setattr(application_logging, "DEBUG_LEVEL", minimum)
    for level in [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ]:
        AliraLogHandler().handle(
            logging.LogRecord(
                "uvicorn.error",
                level,
                "server.py",
                10,
                "message",
                (),
                None,
                func="serve",
            )
        )
    output.flush()
    out, err = capsys.readouterr()
    text = file_text(tmp_path)
    assert [
        level
        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if f"[{level}]" in text
    ] == expected
    assert "[ERROR]" not in out and "[CRITICAL]" not in out
    assert "[ERROR]" in err and "[CRITICAL]" in err


def test_filtered_records_do_not_interpolate_arguments(output, monkeypatch, capsys):
    class Unprintable:
        def __str__(self):
            raise AssertionError("filtered argument formatted")

    monkeypatch.setattr(application_logging, "DEBUG_LEVEL", "ERROR")
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "server.py", 1, "%s", (Unprintable(),), None
    )
    AliraLogHandler().handle(record)
    assert output._submitted == 0
    assert capsys.readouterr() == ("", "")


def test_repeated_configuration_replaces_handlers_and_does_not_enable_asgi_trace(
    output, tmp_path, capsys
):
    from alira.modules.web.application import create_app

    for _ in range(2):
        config = uvicorn.Config(create_app(), log_config=get_uvicorn_log_config())
        config.load()
        assert not isinstance(config.loaded_app, MessageLoggerMiddleware)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1234",
        "GET",
        "/health",
        "1.1",
        200,
    )
    logging.getLogger("uvicorn.error").info("startup-sentinel")
    output.flush()
    out, err = capsys.readouterr()
    assert out.count('127.0.0.1:1234 - "GET /health HTTP/1.1" 200') == 1
    assert out.count("startup-sentinel") == 1
    assert not err
    text = file_text(tmp_path)
    assert text.count("startup-sentinel") == 1
    assert text.count('"GET /health HTTP/1.1" 200') == 1


def test_handler_uses_standard_error_reporting_on_bad_record(output, capsys):
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "server.py", 1, "%d", ("bad",), None
    )
    AliraLogHandler().handle(record)
    assert "Logging error" in capsys.readouterr().err
    assert output._submitted == 0


@pytest.mark.parametrize("fail", [False, True])
def test_logger_lifecycle_drains_service_shutdown_logs_without_outer_context(
    output, tmp_path, monkeypatch, fail
):
    from alira.kernel.lifespan.auto_init import AutoInit

    entry = importlib.import_module("alira.main")
    initialization = importlib.import_module("alira.kernel.initialize")
    from alira.modules.web.application import create_app

    uvicorn.Config(create_app(), log_config=get_uvicorn_log_config())
    source = logging.getLogger("uvicorn.error")
    output.stop()

    class Registry(AutoInit):
        _instance = None

    class Application:
        async def start(self):
            source.info("application-started")

        async def stop(self):
            assert output._thread is not None
            for i in range(10):  # Exercise shutdown output beyond buffer capacity.
                source.info("application-shutdown-%d", i)
            source.info("finished-server-process")

    registry = Registry()
    registry.register(application_logging.logger)
    registry.register(Application())
    monkeypatch.setattr(initialization, "auto_init", registry)
    monkeypatch.setattr(initialization, "initialize", lambda: None)

    async def run(argv=None):
        assert output._thread is None
        async with initialization.lifespan():
            assert output._thread is not None and output._thread.is_alive()
            if fail:
                raise RuntimeError("server failed")

    monkeypatch.setattr(entry, "run", run)
    if fail:
        with pytest.raises(RuntimeError, match="server failed"):
            entry.main()
    else:
        entry.main()
    assert output._thread is None
    assert not output._pending
    text = file_text(tmp_path)
    assert text.count("application-started") == 1
    assert text.count("application-shutdown-") == 10
    assert text.count("finished-server-process") == 1
    assert "Stopping: Logger" in text
    assert output.dropped_records == 0


def test_nested_output_contexts_keep_one_worker_until_outer_exit(output, tmp_path):
    with Logger.file_logging():
        thread = output._thread
        with Logger.file_logging():
            output.enqueue("ALIRA", "inner")
            output.stop()
            assert output._thread is thread
        output.enqueue("ALIRA", "outer")
        assert output._thread is thread
    assert output._thread is None
    assert file_text(tmp_path) == "inner\nouter\n"


def test_standard_trace_records_follow_trace_flag(output, tmp_path, monkeypatch, capsys):
    source = logging.LogRecord("uvicorn.asgi", 5, "server.py", 1, "trace-sentinel", (), None)
    monkeypatch.setattr(application_logging, "TRACE", False)
    AliraLogHandler().handle(source)
    assert output._submitted == 0
    monkeypatch.setattr(application_logging, "TRACE", True)
    AliraLogHandler().handle(source)
    output.flush()

    out, err = capsys.readouterr()
    assert out.count("trace-sentinel") == 1 and not err
    assert file_text(tmp_path).count("trace-sentinel") == 1


def test_preformatted_exception_text_is_retained_once(output, tmp_path, capsys):
    record = logging.LogRecord("uvicorn.error", logging.ERROR, "server.py", 1, "failed", (), None)
    record.exc_text = "ValueError: remote traceback\n"
    AliraLogHandler().handle(record)
    output.flush()

    out, err = capsys.readouterr()
    assert not out and err.count("ValueError: remote traceback") == 1
    assert file_text(tmp_path).count("ValueError: remote traceback") == 1
