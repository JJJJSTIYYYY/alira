"""Resolve and lock dependencies without installing packages."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument(
        "--check", action="store_true", help="Check freshness without modifying files"
    )
    controls.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before locking",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.lock(check=arguments.check, repair=arguments.repair)
