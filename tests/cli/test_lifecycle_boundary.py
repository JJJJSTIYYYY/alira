"""Management is independent of module imports, kernel lifecycle and signals."""

import subprocess
import sys
from zipfile import ZipFile

import pytest

from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.kernel.module.dependencies.uv import EMPTY_LOCK


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["list"],
        ["sync"],
        ["sync", "--no-install"],
        ["add", "demo"],
        ["add", "demo", "--no-install"],
        ["remove", "demo"],
        ["install", "plugin.zip"],
        ["install", "-r", "package.yml"],
        ["uninstall", "demo"],
        *[
            [name, "--help"]
            for name in ("list", "sync", "add", "remove", "install", "uninstall")
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
        )
    with ZipFile(tmp_path / "plugin.zip", "w") as archive:
        archive.writestr(
            "main.py",
            "raise AssertionError('installed entry executed')\nfrom alira.kernel.module import BaseModule\nmodule=BaseModule('plugin','1')\n",
        )
        archive.writestr("src/__init__.py", "")
    (tmp_path / "package.yml").write_text("modules:\n  - source: plugin.zip\n")
    state = tmp_path / "state"
    ledger = DependencyLedger(state)
    ledger.write(
        {
            "format": 1,
            "modules": {
                "demo": {
                    "packages": {},
                    "requirements": None,
                    "requirements_hash": None,
                    "required_modules": [],
                }
            },
        },
        EMPTY_LOCK,
    )
    readonly = arguments == ["list"] or "--help" in arguments
    if readonly or arguments[0] == "sync":
        ledger.path.write_text("{broken-json")
        ledger.lockfile.write_text("broken-lock")
    snapshot = ledger.snapshot()
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
if {readonly!r}:
    DependencyLedger.read = forbidden
from alira.main import main
try:
    main({arguments!r})
except SystemExit as exc:
    assert exc.code == 0
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
    if arguments == ["list"]:
        assert result.stdout.splitlines() == ["NAME", "demo", "pending"]
    if "--help" in arguments:
        assert result.stdout.startswith("usage: alira")
    if readonly:
        assert ledger.snapshot() == snapshot
