"""Remove declarations and exclusive packages, keeping sources by default."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("names", nargs="+", help="Declared module names")
    parser.add_argument(
        "--purge", action="store_true", help="Also send module sources to system trash"
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Update declaration and lock without removing packages",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before continuing",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.remove(
        *arguments.names,
        purge=arguments.purge,
        no_sync=arguments.no_sync,
        repair=arguments.repair,
    )
