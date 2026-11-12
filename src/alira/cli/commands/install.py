"""Install a module source archive, then add its Python dependencies."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "source", nargs="?", help="Local ZIP path or https://github.com/owner/repo"
    )
    parser.add_argument(
        "-r", "--package", help="Install all modules from package.yml, then sync"
    )
    parser.add_argument("--ref", help="GitHub branch, tag or commit")
    parser.set_defaults(command_handler=run, command_parser=parser)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    if bool(arguments.source) == bool(arguments.package):
        arguments.command_parser.error(
            "Specify either a module source or -r package.yml"
        )
    if arguments.package:
        if arguments.ref:
            arguments.command_parser.error("Set ref per module inside package.yml")
        await manager.install_package(arguments.package)
        return
    await manager.install(arguments.source, ref=arguments.ref)
