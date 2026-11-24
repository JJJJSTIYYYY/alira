"""Migrate the legacy global deployment state into this checkout's namespace."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.migrate()
