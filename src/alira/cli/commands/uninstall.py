"""Remove declarations, packages and module sources through the system trash."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("names", nargs="+", help="Module names to uninstall")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before continuing",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.uninstall(*arguments.names, repair=arguments.repair)
