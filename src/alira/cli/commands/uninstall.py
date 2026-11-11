"""Remove module dependencies and send source directories to the system trash."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("names", nargs="+", help="Module names to uninstall")
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.uninstall(*arguments.names)
