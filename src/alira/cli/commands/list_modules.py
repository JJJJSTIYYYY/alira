"""List declaration versions without starting modules or reading deployment records."""

from argparse import ArgumentParser, Namespace

from alira.kernel.module import ModuleManager


def configure(parser: ArgumentParser) -> None:
    parser.set_defaults(command_handler=run)


async def run(arguments: Namespace, manager: ModuleManager) -> None:
    modules = manager.discover()
    names = manager.list()
    width = max(len("NAME"), max(map(len, names), default=0)) + 2
    print(f"{'NAME':<{width}}VERSION")
    for name in names:
        module = modules.get(name)
        version = module.version if module is not None else "-"
        print(f"{name:<{width}}{version}")
