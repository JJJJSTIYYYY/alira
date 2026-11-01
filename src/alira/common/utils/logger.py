"""Application logging and batched background file output."""

import asyncio
import atexit
import json
import logging
import sys
import time
import traceback
from collections import deque
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from threading import Condition, RLock, Thread
from typing import Any, ClassVar

from alira.config.base_config import (
    ALIRA_BASE_DIR,
    DEBUG_LEVEL,
    LOG_BUFFER_SIZE,
    MAX_LOG_FILE_SIZE,
    TRACE,
)

LOG_LEVELS = {
    "DEBUG": 0,
    "INFO": 1,
    "WARN": 2,
    "ERROR": 3,
}


def register_log_level(tag: str, priority: int, *, exist_ok: bool = False):
    """Register a custom log level."""
    if tag in LOG_LEVELS and not exist_ok:
        raise ValueError(f"Log level `{tag}` has been registered.")
    LOG_LEVELS[tag] = priority


def unregister_log_level(tag: str, *, missing_ok: bool = True):
    """Unregister a custom log level."""
    if tag not in LOG_LEVELS and not missing_ok:
        raise ValueError(f"Log level `{tag}` has not been registered.")
    LOG_LEVELS.pop(tag, None)


class LogWriter:
    """Keep I/O off callers; full running buffers apply synchronous backpressure.

    Before start, retain the newest ``capacity`` records and report any eviction.
    A single worker owns rotation. Barriers include all records accepted before
    the call; cancellation of an async caller does not cancel the worker's I/O.
    """

    STARTUP_SUFFIX: ClassVar[str] = datetime.now().strftime("%H%M%S%f")[:-3]

    def __init__(self, root: str, capacity: int, file_size: int, interval: float = 0.2):
        if capacity <= 0 or file_size <= 0 or interval <= 0:
            raise ValueError(
                "Log capacity, file size and flush interval must be positive"
            )
        self.root = Path(root)
        self.capacity = capacity
        self.file_size = file_size
        self.interval = interval
        self._condition = Condition()
        self._lifecycle = RLock()
        self._holds = 0
        self._pending: deque[tuple[int, str, str]] = deque()
        self._thread: Thread | None = None
        self._stopping = False
        self._force = False
        self._submitted = 0
        self._completed = 0
        self._error: Exception | None = None
        self.dropped_records = 0
        self._reported_drops = 0
        self._indices: dict[str, int] = {}
        self._date = ""
        self._date_second: int | None = None

    def enqueue(self, name: str, message: str) -> None:
        with self._condition:
            while len(self._pending) >= self.capacity and self._thread is not None:
                # Shutdown is a cutoff: later records are console-only.
                if self._stopping:
                    return
                self._condition.wait()
            if self._stopping:
                return
            if len(self._pending) >= self.capacity:
                self._pending.popleft()
                self.dropped_records += 1
            self._submitted += 1
            self._pending.append((self._submitted, name, message))
            if len(self._pending) >= self.capacity:
                self._condition.notify_all()

    def start(self) -> None:
        with self._lifecycle:
            self._start()

    @contextmanager
    def running(self) -> Iterator[None]:
        """Keep the writer alive across nested application start/stop calls."""
        with self._lifecycle:
            self._start()
            self._holds += 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._holds -= 1
                if not self._holds:
                    self.stop()

    def _start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._error = None
            self._stopping = False
            thread = Thread(target=self._run, name="alira-log-writer", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise

    def flush(self) -> None:
        # Serialize with start/stop, including a stop whose async caller cancelled.
        with self._lifecycle:
            with self._condition:
                if not self._pending and self._thread is None:
                    self._raise_error()
                    return
            self._start()
            with self._condition:
                target = self._submitted
                self._force = True
                self._condition.notify_all()
                self._condition.wait_for(lambda: self._completed >= target)
                self._raise_error()

    def stop(self) -> None:
        with self._lifecycle:
            if self._holds:
                self.flush()
                return
            with self._condition:
                if self._thread is None and not self._pending:
                    self._raise_error()
                    return
            self._start()
            with self._condition:
                self._stopping = True
                thread = self._thread
                self._condition.notify_all()
            thread.join()
            with self._condition:
                self._thread = None
                self._stopping = False
                self._condition.notify_all()
                self._raise_error()

    def _raise_error(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise RuntimeError("Log file output failed") from error

    def _run(self) -> None:
        while True:
            with self._condition:
                # The periodic wake also flushes a lone record without a signal.
                self._condition.wait_for(
                    lambda: (
                        self._stopping
                        or self._force
                        or len(self._pending) >= self.capacity
                    ),
                    timeout=self.interval,
                )
                cache = self._pending
                self._pending = deque()
                self._force = False
                stopping = self._stopping
                dropped = self.dropped_records - self._reported_drops
                self._reported_drops = self.dropped_records
                self._condition.notify_all()
            if dropped:
                try:
                    print(
                        f"Alira logger: {dropped} pre-start records evicted",
                        file=sys.stderr,
                    )
                except (OSError, ValueError):
                    pass  # A closed console must not kill the file worker.
            if cache:
                try:
                    self.write_batch([(name, line) for _, name, line in cache])
                except Exception as error:  # noqa: BLE001 - keep the writer alive and report at barriers
                    with self._condition:
                        self._error = error
                    try:
                        traceback.print_exc(file=sys.stderr)
                    except (OSError, ValueError):
                        pass  # The barrier still reports the original file error.
                finally:
                    with self._condition:
                        self._completed = cache[-1][0]
                        self._condition.notify_all()
            if stopping:
                return

    def write_batch(self, records: list[tuple[str, str]]) -> None:
        """Worker-only output; split batches at UTF-8 record boundaries.

        A record larger than the limit occupies a file by itself. Successful
        writes reach the OS page cache; this does not promise fsync durability.
        """
        second = int(time.time())
        if second != self._date_second:
            today = datetime.fromtimestamp(second).strftime("%Y-%m-%d")
            if today != self._date:
                self._date = today
                self._indices.clear()
            self._date_second = second
        today = self._date
        grouped: dict[str, list[bytes]] = {}
        for name, message in records:
            grouped.setdefault(name, []).append((message + "\n").encode("utf-8"))
        errors: list[Exception] = []
        for name, lines in grouped.items():
            try:
                self._write_lines(name, today, lines)
            except (OSError, ValueError) as error:
                # A broken logger directory must not suppress the other names.
                errors.append(error)
        if errors:
            raise ExceptionGroup("Unable to write log batch", errors)

    def _write_lines(self, name: str, today: str, lines: list[bytes]) -> None:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        index = self._indices.get(name, 0)
        offset = 0
        while offset < len(lines):
            rotation_suffix = f"_{index}" if index else ""
            path = (
                directory / f"{today}_{LogWriter.STARTUP_SUFFIX}{rotation_suffix}.log"
            )
            with path.open("ab") as output:
                size = output.tell()
                end = offset
                while end < len(lines):
                    length = len(lines[end])
                    if size and size + length > self.file_size:
                        break
                    size += length
                    end += 1
                if end > offset:
                    output.write(b"".join(lines[offset:end]))
                    offset = end
            if offset < len(lines):
                index += 1
        self._indices[name] = index


class Logger:
    """Colored console logging with bounded, batched background file output."""

    _writer: ClassVar[LogWriter] = LogWriter(
        ALIRA_BASE_DIR, LOG_BUFFER_SIZE, MAX_LOG_FILE_SIZE
    )
    _time_cache: ClassVar[tuple[int | None, str]] = (None, "")

    COLOR_CODES: ClassVar[dict[str, str]] = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "purple": "\033[95m",
        "cyan": "\033[96m",
        "white": "\033[97m",
        "gray": "\033[90m",
        "light_gray": "\033[37m",
        "light_yellow": "\033[93;1m",
        "bold": "\033[1m",
        "underline": "\033[4m",
        "reset": "\033[0m",
    }

    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "info": "light_gray",
        "warning": "yellow",
        "error": "red",
        "exception": "red",
        "critical": "red",
        "trace": "cyan",
        "success": "green",
        "debug": "cyan",
    }

    def __init__(self, name="Logger", show_time=True, show_level=True):
        """
        Args:
            name (str): Logger name
            show_time (bool): If to show timestamp
            show_level (bool): If to show log level
        """
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or any(char in name for char in ("/", "\\", "\x00"))
        ):
            raise ValueError("Logger name must be a non-empty directory name")
        self.name = name
        self.show_time = show_time
        self.show_level = show_level

    def _get_caller_info(self) -> tuple[str, str, str]:
        """
        Get caller module/class/function information.

        Returns:
            tuple[str, str, str]:
                (module_name, class_name, function_name)
        """
        frame = sys._getframe(1)
        try:
            while frame:
                # Inspect locals only after skipping the logger's own frames.
                if frame.f_globals is globals():
                    frame = frame.f_back
                    continue

                module_name = frame.f_globals.get("__name__", "UNKNOWN")
                module_name = module_name.rsplit(".", 1)[-1]

                function_name = frame.f_code.co_name

                class_name = "GLOBAL"

                if "self" in frame.f_locals:
                    class_name = frame.f_locals["self"].__class__.__name__
                elif isinstance(frame.f_locals.get("cls"), type):
                    class_name = frame.f_locals["cls"].__name__

                return module_name, class_name, function_name

            return "UNKNOWN", "GLOBAL", "UNKNOWN"

        finally:
            del frame

    def _format_message(self, message, *args):
        if not args:
            return str(message)

        message = str(message)
        try:
            return message % args
        except (TypeError, ValueError):
            parts = [str(message)]
            parts.extend(str(arg) for arg in args)
            return " ".join(parts)

    def _format_kwargs(self, **kwargs):
        if not kwargs:
            return ""

        parts = []
        for key, value in kwargs.items():
            if isinstance(value, (dict, list, tuple, set)):
                formatted_value = repr(value)
            elif isinstance(value, str):
                if " " in value:
                    formatted_value = f'"{value}"'
                else:
                    formatted_value = value
            else:
                formatted_value = str(value)

            parts.append(f"{key}={formatted_value}")

        return " | ".join(parts)

    def _get_formatted_message(self, level, message, *args, **kwargs):
        formatted = self._format_message(message, *args)
        if kwargs:
            formatted += f" ({self._format_kwargs(**kwargs)})"
        return self._format_entry(level, formatted, " ".join(self._get_caller_info()))

    def _format_entry(
        self, level: str, message: str, source: str, created: float | None = None
    ) -> str:
        parts = []

        if self.show_time:
            second = int(time.time() if created is None else created)
            cached = Logger._time_cache
            if cached[0] != second:
                cached = (
                    second,
                    datetime.fromtimestamp(second).strftime("%Y-%m-%d %H:%M:%S"),
                )
                Logger._time_cache = cached
            parts.append(cached[1])

        parts.append(f"[{self.name}]")

        if self.show_level:
            parts.append(f"[{level.upper()}]")

        parts.append(f"[{source}]:")
        parts.append(message)
        return " ".join(parts)

    def _colorize(self, text, color_name):
        color_code = self.COLOR_CODES.get(color_name, self.COLOR_CODES["white"])
        return f"{color_code}{text}{self.COLOR_CODES['reset']}"

    def _log(self, level, message, *args, color_name=None, **kwargs):
        formatted_message = self._get_formatted_message(level, message, *args, **kwargs)

        if level == "exception" and sys.exc_info()[0] is not None:
            formatted_message += "\n" + traceback.format_exc().rstrip()
        self._emit(level, formatted_message, color_name)

    def _emit(
        self, level: str, formatted_message: str, color_name: str | None = None
    ) -> None:
        if color_name is None:
            color_name = self.LEVEL_COLORS.get(level, "white")
        Logger._writer.enqueue(self.name, formatted_message)

        colored_message = self._colorize(formatted_message, color_name)

        print(
            colored_message,
            file=sys.stderr
            if level in {"error", "exception", "critical"}
            else sys.stdout,
        )

    def log_record(self, record: logging.LogRecord) -> None:
        """Render a standard record using its original source, time and traceback."""
        if record.levelno >= logging.ERROR:
            priority = LOG_LEVELS["ERROR"]
        elif record.levelno >= logging.WARNING:
            priority = LOG_LEVELS["WARN"]
        elif record.levelno >= logging.INFO:
            priority = LOG_LEVELS["INFO"]
        else:
            priority = LOG_LEVELS["DEBUG"]
        if priority < LOG_LEVELS[DEBUG_LEVEL]:
            return
        if record.levelno < logging.DEBUG and not TRACE:
            return
        message = record.getMessage()
        if record.exc_info:
            message += (
                "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )
        elif record.exc_text:
            message += "\n" + record.exc_text.rstrip()
        if record.stack_info:
            message += "\n" + record.stack_info.rstrip()
        source = f"{record.name} {record.module} {record.funcName}:{record.lineno}"
        level = record.levelname.lower()
        self._emit(level, self._format_entry(level, message, source, record.created))

    def info(self, message, *args, **kwargs):
        if LOG_LEVELS["INFO"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("info", message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        if LOG_LEVELS["WARN"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("warning", message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        if LOG_LEVELS["ERROR"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("error", message, *args, **kwargs)

    def exception(self, message, *args, **kwargs):
        if LOG_LEVELS["ERROR"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("exception", message, *args, **kwargs)

    def success(self, message, *args, **kwargs):
        if LOG_LEVELS["INFO"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("success", message, *args, **kwargs)

    def debug(self, message, *args, **kwargs):
        if LOG_LEVELS["DEBUG"] < LOG_LEVELS[DEBUG_LEVEL]:
            return
        self._log("debug", message, *args, **kwargs)

    def custom(self, message, *args, level="DEBUG", color="light_yellow", **kwargs):
        if level not in LOG_LEVELS:
            return

        if LOG_LEVELS[level] < LOG_LEVELS[DEBUG_LEVEL]:
            return

        self._log(level.lower(), message, *args, color_name=color, **kwargs)

    def separator(self, length=50, char="-", color="light_yellow", *args, **kwargs):
        separator_line = char * length
        self.custom(separator_line, *args, level="INFO", color=color, **kwargs)

    def trace(self, message=None):
        if not TRACE:
            return
        if not message:
            message = "Enter"
        self._log("debug", message)

    async def write_log(self, log_folder: str, log_file: str, message: Any):
        """
        Append message to log file.
        """

        await asyncio.to_thread(self._write_log_sync, log_folder, log_file, message)

    def _write_log_sync(self, log_folder: str, log_file: str, message: Any):
        if not log_folder or not log_folder.strip():
            log_folder = "logs"

        if not log_file or not log_file.strip():
            log_file = str(time.time())

        suffix = ".jsonl" if isinstance(message, dict) else ".log"
        base = Path(ALIRA_BASE_DIR).resolve()
        destination = (base / log_folder / f"{log_file}{suffix}").resolve()
        if not destination.is_relative_to(base):
            raise ValueError("Log path must stay inside ALIRA_BASE_DIR")
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = (
            json.dumps(message, ensure_ascii=False)
            if isinstance(message, dict)
            else str(message)
        )
        with destination.open("a", encoding="utf-8") as output:
            output.write(text + "\n")

    @classmethod
    async def start(cls) -> None:
        """Start the shared writer; safe across threads and event loops."""
        await asyncio.to_thread(Logger._writer.start)

    @classmethod
    async def stop(cls) -> None:
        """Drain accepted records and join the writer before returning."""
        await asyncio.to_thread(Logger._writer.stop)

    @classmethod
    async def flush(cls) -> None:
        """Wait for records already accepted; surface any file output failure."""
        await asyncio.to_thread(Logger._writer.flush)

    @classmethod
    def file_logging(cls) -> AbstractContextManager[None]:
        """Own file output until the entire server, including final logs, exits."""
        return Logger._writer.running()


class AliraLogHandler(logging.Handler):
    """Forward standard-library records to the application logger."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            logger.log_record(record)
        except Exception:  # noqa: BLE001 - use the standard Handler error path
            self.handleError(record)


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


def _shutdown() -> None:
    try:
        Logger._writer.stop()
    except RuntimeError:
        traceback.print_exc(file=sys.stderr)


atexit.register(_shutdown)

logger = Logger(name="ALIRA", show_time=True, show_level=True)
