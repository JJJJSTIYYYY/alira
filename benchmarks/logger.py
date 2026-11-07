"""Compare equal file work and isolate logging transport overhead.

Run with: .venv/bin/python benchmarks/logger.py --records 20000 --rounds 6
Times include draining every record, and each file run verifies exact output.
No fsync is performed. Console/formatting are measured separately.
"""

import argparse
import asyncio
import contextlib
import gc
import io
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from alira.utils.logger import Logger, LogWriter

MESSAGE = "中文 " + "x" * 170


async def measure(mode, root, count, batch_size):
    writer = LogWriter(str(root), batch_size, 1 << 40)
    line = ("test", MESSAGE)
    batch = [line] * batch_size
    if mode == "thread_transport":
        writer.write_batch = lambda records: None
    if mode.startswith("thread_"):
        writer.start()
    start = time.perf_counter_ns()
    if mode == "direct_each":
        for _ in range(count):
            writer.write_batch([line])
    elif mode == "direct_batch":
        for _ in range(count // batch_size):
            writer.write_batch(batch)
    elif mode == "executor_batch":
        for _ in range(count // batch_size):
            await asyncio.to_thread(writer.write_batch, batch)
    else:
        for _ in range(count):
            writer.enqueue(*line)
    producer_end = time.perf_counter_ns()
    if mode.startswith("thread_"):
        await asyncio.to_thread(writer.stop)
    end = time.perf_counter_ns()
    if mode != "thread_transport":
        output = b"".join(path.read_bytes() for path in (root / "test").glob("*.log"))
        assert output == ((MESSAGE + "\n") * count).encode(), mode
    return ((producer_end - start) / count / 1000, (end - start) / count / 1000)


def measure_formatting(count):
    """Console output goes to memory; enqueue and file output are excluded."""
    log = Logger("test")
    original = Logger._writer

    class Sink:
        def enqueue(self, *args):
            pass

    Logger._writer = Sink()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            start = time.perf_counter_ns()
            for i in range(count):
                log.info("message %s", i, key="value")
            return (time.perf_counter_ns() - start) / count / 1000
    finally:
        Logger._writer = original


async def main(args):
    modes = [
        "direct_each",
        "direct_batch",
        "executor_batch",
        "thread_batch",
        "thread_transport",
    ]
    samples = {mode: [] for mode in modes}
    # Warm the executor and implementations; alternate forward/reverse order.
    with tempfile.TemporaryDirectory(prefix="alira-log-bench-") as folder:
        root = Path(folder)
        for mode in modes:
            await measure(mode, root / f"warm-{mode}", args.batch * 2, args.batch)
        for repeat in range(args.rounds):
            order = modes if repeat % 2 == 0 else list(reversed(modes))
            for mode in order:
                gc.collect()
                sample = await measure(
                    mode, root / f"{repeat}-{mode}", args.records, args.batch
                )
                samples[mode].append(sample)
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "records": args.records,
                "batch": args.batch,
                "rounds": args.rounds,
                "microseconds_per_record": {
                    mode: {
                        "producer_median": round(
                            statistics.median(s[0] for s in values), 3
                        ),
                        "drained_median": round(
                            statistics.median(s[1] for s in values), 3
                        ),
                        "drained_min": round(min(s[1] for s in values), 3),
                        "drained_max": round(max(s[1] for s in values), 3),
                    }
                    for mode, values in samples.items()
                },
                "format_and_console_to_memory_us": round(
                    measure_formatting(args.records), 3
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20000)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()
    if min(args.records, args.rounds, args.batch) <= 0 or args.records % args.batch:
        parser.error("Use positive values and a record count divisible by batch size")
    asyncio.run(main(args))
