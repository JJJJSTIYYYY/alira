"""Declare module names or sources and resolve their dependency lock."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "names", nargs="*", help="Module names, local ZIPs/directories or GitHub URLs"
    )
    parser.add_argument(
        "--manifest", metavar="FILE", help="Read module sources from a YAML manifest"
    )
    parser.add_argument("--ref", help="GitHub branch, tag or commit")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Update declaration and lock without installing packages",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore an interrupted operation before continuing",
    )
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    await manager.add(
        *arguments.names,
        manifest=arguments.manifest,
        ref=arguments.ref,
        no_sync=arguments.no_sync,
        repair=arguments.repair,
    )
