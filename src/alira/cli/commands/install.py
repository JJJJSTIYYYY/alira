"""Compatibility entry point for adding module sources."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("source", nargs="?", help="Local ZIP/directory or GitHub URL")
    parser.add_argument(
        "--manifest",
        "--package",
        "-r",
        dest="manifest",
        metavar="FILE",
        help="Add sources from a YAML manifest",
    )
    parser.add_argument("--ref", help="GitHub branch, tag or commit")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Write the lock without installing packages",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before continuing",
    )
    parser.set_defaults(command_handler=run, command_parser=parser)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    if bool(arguments.source) == bool(arguments.manifest):
        arguments.command_parser.error(
            "Specify either a module source or --manifest FILE"
        )
    if arguments.manifest:
        if arguments.ref:
            arguments.command_parser.error("Set ref per module inside the manifest")
        await manager.install_package(
            arguments.manifest, no_sync=arguments.no_sync, repair=arguments.repair
        )
    else:
        await manager.install(
            arguments.source,
            ref=arguments.ref,
            no_sync=arguments.no_sync,
            repair=arguments.repair,
        )
