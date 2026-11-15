"""Initialize a module source skeleton without starting application services."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("name", help="Name of the new module")
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    directory = await manager.init(arguments.name)
    print(f"Initialized module: {directory}")
