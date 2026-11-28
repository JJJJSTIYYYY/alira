"""Build CLI syntax without discovering or importing modules."""

from argparse import ArgumentParser

from alira.cli.commands import (
    add,
    init,
    install,
    list_modules,
    lock,
    migrate,
    remove,
    sync,
    tree,
    uninstall,
)


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
    controls = runtime.add_mutually_exclusive_group()
    controls.add_argument(
        "--no-sync",
        action="store_true",
        help="Run without checking or synchronizing dependencies",
    )
    controls.add_argument(
        "--sync",
        action="store_true",
        help="Explicitly synchronize the environment before running",
    )
    freshness = runtime.add_mutually_exclusive_group()
    freshness.add_argument(
        "--locked",
        action="store_true",
        help="Require an up-to-date lock; never resolve",
    )
    freshness.add_argument(
        "--frozen",
        action="store_true",
        help="Install from the existing lock without checking freshness",
    )
    for name, description, command in (
        ("init", "Create a new module source skeleton", init),
        ("list", "List module names and versions without starting them", list_modules),
        ("lock", "Resolve module dependencies without installing", lock),
        ("sync", "Synchronize sources and Python packages from the lock", sync),
        ("tree", "Show module and Python package dependencies", tree),
        ("migrate", "Migrate legacy global state into this source checkout", migrate),
        ("add", "Declare modules and install their dependencies", add),
        ("remove", "Release dependencies, preserving module sources", remove),
        ("install", "Install a module or a package.yml source set", install),
        (
            "uninstall",
            "Remove modules; prompt for permanent deletion if the system trash fails",
            uninstall,
        ),
    ):
        command.configure(
            commands.add_parser(name, help=description, allow_abbrev=False)
        )
    return parser
