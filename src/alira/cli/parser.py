"""Build CLI syntax without discovering or importing modules."""

from argparse import ArgumentParser

from alira.cli.commands import add, init, install, list_modules, remove, sync, uninstall


def build_parser() -> ArgumentParser:
    """Build management commands and application startup syntax.

    Building the parser does not discover modules or import their entry points.
    A subcommand is required, and runtime options belong exclusively to ``run``.
    """
    parser = ArgumentParser(prog="alira", allow_abbrev=False)
    parser.set_defaults(command_handler=None)
    commands = parser.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser(
        "run", help="Start the kernel and selected modules", allow_abbrev=False
    )
    runtime.add_argument(
        "modules",
        nargs="*",
        help="Modules to start together with their prerequisites",
    )
    runtime.add_argument(
        "-i",
        "--install-missing",
        action="store_true",
        help="Explicitly deploy selected module dependencies before starting",
    )
    for name, description, command in (
        ("init", "Create a new module source skeleton", init),
        ("list", "List module names and versions without starting them", list_modules),
        (
            "sync",
            "Rebuild all module dependencies and synchronize Python packages",
            sync,
        ),
        ("add", "Add existing modules to dependency management", add),
        ("remove", "Release dependencies, preserving module sources", remove),
        ("install", "Install a module or a package.yml source set", install),
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
