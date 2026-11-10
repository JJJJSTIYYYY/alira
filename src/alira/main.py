"""Process entry point for core services and standard application modules."""

import asyncio
from argparse import ArgumentParser
from collections.abc import Sequence
import signal

from alira.kernel.initialize import lifespan
from alira.kernel.module import module_manager
from alira.shared.application.version import print_logo


async def run(argv: Sequence[str] | None = None) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    try:
        async with lifespan():
            parser = ArgumentParser(prog="alira", allow_abbrev=False)
            parser.set_defaults(modules=[])
            for name, module in module_manager.modules.items():
                parser.add_argument(
                    f"--{name}", dest="modules", action="append_const", const=name,
                    help=module.description or f"Load module {name}",
                )
            selected = parser.parse_args(argv).modules
            if selected:
                await module_manager.load(*selected)
            await stopped.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: Sequence[str] | None = None) -> None:
    """Run services whose lifecycle includes starting and stopping logging."""
    print_logo()
    try:
        asyncio.run(run(argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
