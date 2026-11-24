"""Synchronize module sources and packages from the dependency lock."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument(
        "--locked", action="store_true", help="Fail if the lock is missing or outdated"
    )
    controls.add_argument(
        "--frozen",
        action="store_true",
        help="Use the existing lock without checking declarations",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before continuing",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.sync(
        locked=arguments.locked, frozen=arguments.frozen, repair=arguments.repair
    )
