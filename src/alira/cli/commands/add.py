"""Add existing modules to dependency management."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("names", nargs="+", help="Existing module names")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Update dependency records without modifying Python packages",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.add(*arguments.names, no_install=arguments.no_install)
