"""Parse commands before choosing management operations or application startup."""

import asyncio
import sys
from collections.abc import Sequence

from alira.cli.parser import build_parser
from alira.kernel.module import module_manager
from alira.kernel.module.dependencies import DependencyError


async def run(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command_handler is not None:
        if arguments.modules or arguments.install_missing:
            parser.error("Module commands cannot be combined with runtime flags")
        await arguments.command_handler(arguments, module_manager)
        return

    module_manager.discover()
    unknown = set(arguments.modules) - module_manager.modules.keys()
    if unknown:
        parser.error(
            f"--modules: unknown or unavailable modules: {', '.join(sorted(unknown))}"
        )

    from alira.cli.application import run_application

    await run_application(
        module_manager, arguments.modules, install_missing=arguments.install_missing
    )


def main(argv: Sequence[str] | None = None) -> None:
    try:
        asyncio.run(run(argv))
    except KeyboardInterrupt:
        pass
    except (DependencyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except BaseExceptionGroup as exc:
        expected, unexpected = exc.split((DependencyError, OSError))
        if unexpected is not None:
            raise

        def report(group):
            print(group.message, file=sys.stderr)
            for error in group.exceptions:
                if isinstance(error, BaseExceptionGroup):
                    report(error)
                else:
                    print(str(error), file=sys.stderr)

        report(expected)
        raise SystemExit(1) from None
