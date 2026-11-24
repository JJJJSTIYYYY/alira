"""Management is independent of module imports, kernel lifecycle and signals."""

import subprocess
import sys
from zipfile import ZipFile

import pytest

from alira.kernel.module.dependencies.ledger import DependencyLedger


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["-h"],
        ["run", "--help"],
        ["run", "-h"],
        ["init", "example"],
        ["sync"],
        ["sync", "--locked"],
        ["sync", "--frozen"],
        ["lock"],
        ["lock", "--check"],
        ["tree"],
        ["tree", "--frozen"],
        ["add", "demo"],
        ["add", "demo", "--no-sync"],
        ["remove", "demo"],
        ["install", "plugin.zip"],
        ["install", "-r", "package.yml"],
        ["install", "--manifest", "package.yml"],
        ["add", "--manifest", "package.yml"],
        ["uninstall", "demo"],
        *[
            [name, flag]
            for name in (
                "init",
                "list",
                "sync",
                "lock",
                "tree",
                "migrate",
                "add",
                "remove",
                "install",
                "uninstall",
            )
            for flag in ("--help", "-h")
        ],
    ],
)
def test_management_never_imports_entries_or_enters_kernel_lifecycle(
    tmp_path, arguments
):
    modules = tmp_path / "cli_modules"
    for name in ("demo", "pending"):
        folder = modules / name
        (folder / "src").mkdir(parents=True)
        (folder / "main.py").write_text(
            "raise AssertionError('module entry executed')\n"
            "from alira.kernel.module import BaseModule\n"
            f"module=BaseModule({name!r},'1')\n"
        )
    with ZipFile(tmp_path / "plugin.zip", "w") as archive:
        archive.writestr(
            "main.py",
            "raise AssertionError('installed entry executed')\n"
            "from alira.kernel.module import BaseModule\n"
            "module=BaseModule('plugin','1')\n",
        )
        archive.writestr("src/__init__.py", "")
    (tmp_path / "package.yml").write_text("modules:\n  - source: plugin.zip\n")
    state = tmp_path / "state"
    ledger = DependencyLedger(state)
    ledger.write_declaration(
        {
            "format": 2,
            "requires-kernel": ">=3,<4",
            "modules": {
                "demo": {
                    "version": "1",
                    "source": {"type": "builtin"},
                    "required-modules": [],
                    "requirements": None,
                    "requirements-hash": None,
                }
            },
        }
    )
    help_requested = any(flag in arguments for flag in ("--help", "-h"))
    if help_requested:
        ledger.path.write_text("{broken-json")
        ledger.lockfile.write_text("broken-lock")
    trash = tmp_path / "system-trash"
    trash.mkdir()
    script = f"""
import asyncio, importlib.abc, shutil, sys
from pathlib import Path

class NoApplicationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "alira.cli.application" or fullname == "alira.modules" or fullname.startswith(("alira.modules.", "cli_modules")):
            raise AssertionError("management imported application or module code: " + fullname)
sys.meta_path.insert(0, NoApplicationImports())
from alira.kernel.comlink import ApixisCore
from alira.kernel.lifespan import AutoInit, ResourceCleaner, auto_init, resource_cleaner
from alira.shared.logger import Logger
from alira.kernel.module import ModuleManager, module_manager
from alira.kernel.module.dependencies.ledger import DependencyLedger
import send2trash
send2trash.send2trash = lambda path: shutil.move(path, Path({str(trash)!r}) / Path(path).name)

def forbidden(*args, **kwargs):
    raise AssertionError("management entered the kernel lifecycle")
for service in (AutoInit, ApixisCore, ResourceCleaner, Logger, ModuleManager):
    service.start = forbidden
    service.stop = forbidden
AutoInit.register = forbidden
ModuleManager.discover = forbidden
loop = asyncio.new_event_loop()
loop_type = type(loop)
loop.close()
loop_type.add_signal_handler = forbidden
loop_type.remove_signal_handler = forbidden
module_manager.modules_dir = Path({str(modules)!r})
module_manager._dependencies.ledger = DependencyLedger(Path({str(state)!r}))
module_manager._dependencies.installed = lambda: {{}}
if {help_requested!r}:
    DependencyLedger.read = forbidden
    DependencyLedger.read_lock = forbidden
elif {arguments[0] != "init"!r}:
    # Prepare a current lock once, independently of the command being exercised.
    asyncio.run(module_manager.lock())
snapshot = module_manager._dependencies.ledger.snapshot()
from alira.main import main
try:
    main({arguments!r})
except SystemExit as exc:
    assert exc.code == 0
if {help_requested or arguments[0] == "init" or arguments == ["lock", "--check"] or arguments == ["sync", "--locked"] or arguments == ["sync", "--frozen"] or arguments == ["tree", "--frozen"]!r}:
    assert module_manager._dependencies.ledger.snapshot() == snapshot
assert auto_init._services == []
assert resource_cleaner._task is None
assert Logger._writer._thread is None
assert not module_manager.loaded
assert "alira.cli.application" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    assert "Registered service:" not in result.stdout
    assert "Started:" not in result.stdout
    assert "\033" not in result.stdout
    if arguments == ["init", "example"]:
        assert (modules / "example/src/shared/events.py").is_file()
    if help_requested:
        assert result.stdout.startswith("usage: alira")
