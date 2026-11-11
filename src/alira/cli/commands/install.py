"""Install a module source archive, then add its Python dependencies."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "source", help="Local ZIP path or https://github.com/owner/repo"
    )
    parser.add_argument("--ref", help="GitHub branch, tag or commit")
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.install(arguments.source, ref=arguments.ref)
