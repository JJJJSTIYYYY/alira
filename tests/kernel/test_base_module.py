import asyncio
import os
import sys
from importlib import import_module

import pytest

from alira.kernel.module.base_module import BaseModule


@pytest.fixture
def installer(tmp_path, monkeypatch):
    executable = tmp_path / "uv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:3] == ['pip', 'install']\n"
        "assert sys.argv[sys.argv.index('--python') + 1] == sys.executable\n"
        "requirements = Path(sys.argv[sys.argv.index('--requirement') + 1])\n"
        "assert Path.cwd() == requirements.parent\n"
        "result = requirements.read_text()\n"
        "Path('installer-started').write_text(str(os.getpid()))\n"
        "if result == 'wait': time.sleep(60)\n"
        "sys.exit(int(result))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    requirements = tmp_path / "module with spaces" / "requirements.txt"
    requirements.parent.mkdir()
    return requirements


async def test_install_without_requirements(monkeypatch):
    monkeypatch.setenv("PATH", "")
    module = BaseModule("module_without_dependencies", "1.0")
    assert await module.install() is True


@pytest.mark.parametrize("exit_code, expected", [(0, True), (1, False)])
async def test_install_in_current_environment(installer, exit_code, expected):
    installer.write_text(str(exit_code))
    module = BaseModule("example", "1.0", requirements_txt=str(installer))
    assert await module.install() is expected
    assert (installer.parent / "installer-started").is_file()


async def test_install_when_uv_is_unavailable(installer, monkeypatch):
    installer.write_text("0")
    monkeypatch.setenv("PATH", "")
    module = BaseModule("example", "1.0", requirements_txt=str(installer))
    assert await module.install() is False


@pytest.mark.parametrize("requirements_txt", [None, "example/requirements.txt"])
async def test_install_finds_requirements_under_modules(installer, monkeypatch, requirements_txt):
    alira_dir = installer.parent / "alira"
    requirements = alira_dir / "modules" / "example" / "requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("0")
    monkeypatch.setattr(
        import_module("alira.kernel.module.base_module"), "ALIRA_DIR", alira_dir
    )
    module = BaseModule("example", "1.0", requirements_txt=requirements_txt)
    assert await module.install() is True
    assert (requirements.parent / "installer-started").is_file()


async def test_cancellation_stops_installer(installer):
    installer.write_text("wait")
    module = BaseModule("example", "1.0", requirements_txt=str(installer))
    task = asyncio.create_task(module.install())
    started = installer.parent / "installer-started"

    async def wait_for_installer():
        while not started.is_file():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_installer(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), 0)
