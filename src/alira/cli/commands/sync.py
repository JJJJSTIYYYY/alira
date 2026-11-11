"""Rebuild all module dependency records and synchronize the environment."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Resolve and write records without installing, updating or uninstalling packages",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.sync(no_install=arguments.no_install)
