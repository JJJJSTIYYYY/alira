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
        await arguments.command_handler(arguments, module_manager)
        return

    await module_manager.prepare_run(
        *arguments.modules,
        no_sync=arguments.no_sync,
        locked=arguments.locked,
        frozen=arguments.frozen,
        force_sync=arguments.sync,
    )
    module_manager.discover()
    unknown = set(arguments.modules) - module_manager.modules.keys()
    if unknown:
        parser.error(f"Unknown or unavailable modules: {', '.join(sorted(unknown))}")

    from alira.cli.application import run_application

    await run_application(module_manager, arguments.modules)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        asyncio.run(run(argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None
    except (DependencyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(getattr(exc, "exit_code", 1)) from None
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

        def exit_code(group):
            return max(
                exit_code(error)
                if isinstance(error, BaseExceptionGroup)
                else getattr(error, "exit_code", 1)
                for error in group.exceptions
            )

        raise SystemExit(exit_code(expected)) from None
