"""Build CLI syntax without discovering or importing modules."""

from argparse import ArgumentParser

from alira.cli.commands import add, install, list_modules, remove, sync, uninstall


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="alira", allow_abbrev=False)
    parser.set_defaults(command_handler=None)
    parser.add_argument(
        "--modules",
        nargs="+",
        action="extend",
        default=[],
        help="Start selected modules and their prerequisites",
    )
    parser.add_argument(
        "--install-missing",
        action="store_true",
        help="Explicitly deploy selected module dependencies before starting",
    )
    commands = parser.add_subparsers(dest="command")
    for name, description, command in (
        ("list", "List module source directories", list_modules),
        (
            "sync",
            "Rebuild all module dependencies and synchronize Python packages",
            sync,
        ),
        ("add", "Add existing modules to dependency management", add),
        ("remove", "Release dependencies, preserving module sources", remove),
        ("install", "Install a module from a local ZIP or GitHub", install),
        (
            "uninstall",
            "Remove dependencies and send module sources to the system trash",
            uninstall,
        ),
    ):
        command.configure(
            commands.add_parser(name, help=description, allow_abbrev=False)
        )
    return parser
