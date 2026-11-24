"""Show module requirements and their locked Python package closures."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument(
        "--locked", action="store_true", help="Fail if the lock is outdated"
    )
    controls.add_argument(
        "--frozen",
        action="store_true",
        help="Read the existing lock without checking freshness",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    if not arguments.frozen:
        await manager.lock(check=arguments.locked)
    print(manager.tree(frozen=arguments.frozen))
