"""Real CLI deployment ownership survives independent Python processes."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from zipfile import ZipFile

import pytest

from alira.kernel.module.dependencies import run_uv
from tests.kernel.module.dependencies.test_uv import environment as environment  # noqa: PLC0414 - export the shared pytest fixture
from tests.kernel.module.dependencies.test_uv import installed


@pytest.fixture
def deployment_cli(environment):
    root, wheelhouse, site = environment
    modules = root / "cli_modules"
    demo = modules / "demo"
    demo.mkdir(parents=True)
    (modules / "__init__.py").touch()
    (demo / "__init__.py").touch()
    (demo / "src").mkdir()
    (demo / "src/__init__.py").touch()
    (demo / "requirements.txt").write_text(
        f"--no-index\n--find-links {wheelhouse}\nleft-pkg==1.0\n"
    )
    (demo / "main.py").write_text(
        "from pathlib import Path\n"
        "from alira.kernel.module import BaseModule\n"
        "class Demo(BaseModule):\n"
        "    async def startup(self):\n"
        "        import left_pkg, shared_pkg\n"
        f"        assert Path(left_pkg.__file__).is_relative_to(Path({str(site)!r}))\n"
        f"        Path({str(root / 'ready')!r}).write_text('ready')\n"
        "    async def shutdown(self):\n"
        f"        Path({str(root / 'stopped')!r}).write_text('stopped')\n"
        "module = Demo('demo', '1.0')\n"
    )
    (root / "config.yaml").write_text(
        json.dumps(
            {
                "SERVER": {"base_dir": str(root / "data")},
            }
        )
    )
    script = root / "run_cli.py"
    script.write_text(
        "from importlib.metadata import distributions\n"
        "from pathlib import Path\n"
        "import sys\n"
        "from packaging.utils import canonicalize_name\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from alira.kernel.module import module_manager\n"
        "from alira.kernel.module.dependencies.ledger import DependencyLedger\n"
        f"module_manager.modules_dir = Path({str(modules)!r})\n"
        "module_manager.package = 'cli_modules'\n"
        f"module_manager._dependencies.ledger = DependencyLedger(Path({str(root / 'state')!r}))\n"
        "module_manager._dependencies.installed = lambda: {\n"
        "    canonicalize_name(dist.metadata['Name']): dist.version\n"
        f"    for dist in distributions(path=[{str(site)!r}])\n"
        "}\n"
        "module_manager._dependencies.restore()\n"
        "from alira import main\n"
        "main.main(sys.argv[1:])\n"
    )
    return root, site, script


def run_cli(root, script, *arguments, env=None):
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


async def test_cli_deployment_and_removal_survive_process_restarts(deployment_cli):
    root, site, script = deployment_cli
    expected = {"left-pkg": "1.0", "shared-pkg": "2.0"}
    added = run_cli(root, script, "add", "demo")
    assert added.returncode == 0, added.stdout + added.stderr
    assert installed(site) == expected
    ledger = root / "state/modules.json"
    deployed = json.loads(ledger.read_text())
    assert deployed["modules"]["demo"]["packages"] == expected
    assert (root / "state/pylock.toml").is_file()
    original_ledger = ledger.read_bytes()
    assert not (root / "ready").exists()

    # An already deployed module must start without an installer executable.
    runtime_environment = os.environ | {"PATH": str(root / "no-executables")}
    process = subprocess.Popen(  # noqa: ASYNC220 - exercise an independent CLI runtime process
        [sys.executable, str(script), "--modules", "demo"],
        cwd=root,
        env=runtime_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        async with asyncio.timeout(5):
            while not (root / "ready").exists():
                if process.poll() is not None:
                    out, err = process.communicate()
                    pytest.fail(f"Module exited before startup: {out}\n{err}")
                await asyncio.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        out, err = await asyncio.to_thread(process.communicate, timeout=5)
        assert process.returncode == 0, out + err
        assert (root / "stopped").read_text() == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.communicate, timeout=5)
    assert installed(site) == expected
    assert ledger.read_bytes() == original_ledger

    removed = run_cli(root, script, "remove", "demo")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert installed(site) == {}
    assert json.loads(ledger.read_text())["modules"] == {}


async def test_cli_reports_removed_dependency_and_repair_command(deployment_cli):
    root, site, script = deployment_cli
    added = run_cli(root, script, "add", "demo")
    assert added.returncode == 0, added.stdout + added.stderr
    ledger = root / "state/modules.json"
    original_ledger = ledger.read_bytes()
    await run_uv("uninstall", "shared-pkg", cwd=root)
    assert installed(site) == {"left-pkg": "1.0"}

    failed = run_cli(
        root,
        script,
        "--modules",
        "demo",
        env=os.environ | {"PATH": str(root / "no-executables")},
    )
    assert failed.returncode != 0
    assert "module 'demo'" in failed.stderr
    assert "missing dependency shared-pkg==2.0" in failed.stderr
    assert "uv run alira sync" in failed.stderr
    assert "Traceback" not in failed.stderr
    assert not (root / "ready").exists()
    assert installed(site) == {"left-pkg": "1.0"}
    assert ledger.read_bytes() == original_ledger


async def test_sync_includes_pending_modules_and_list_never_reads_deployments(
    deployment_cli,
):
    root, site, script = deployment_cli
    modules = root / "cli_modules"
    pending = modules / "pending"
    pending.mkdir()
    (pending / "main.py").write_text("raise RuntimeError('must not execute')\n")
    (pending / "requirements.txt").write_text(
        f"--no-index\n--find-links {root / 'wheels'}\nleaf-pkg==1.0\n"
    )
    no_installer = os.environ | {"PATH": str(root / "no-executables")}
    listed = run_cli(root, script, "list", env=no_installer)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines() == ["NAME", "demo", "pending"]
    synced = run_cli(root, script, "sync")
    assert synced.returncode == 0, synced.stderr
    assert installed(site) == {
        "left-pkg": "1.0",
        "shared-pkg": "2.0",
        "leaf-pkg": "1.0",
    }
    assert set(json.loads((root / "state/modules.json").read_text())["modules"]) == {
        "demo",
        "pending",
    }
    assert not (root / "ready").exists()


async def test_install_adds_dependencies_and_remove_preserves_sources(deployment_cli):
    root, site, script = deployment_cli
    directory = root / "cli_modules/demo"
    archive = root / "demo.zip"
    with ZipFile(archive, "w") as package:
        for path in directory.rglob("*"):
            if path.is_file():
                package.write(path, "demo/" + path.relative_to(directory).as_posix())
    shutil.rmtree(directory)
    installed_module = run_cli(root, script, "install", str(archive))
    assert installed_module.returncode == 0, installed_module.stderr
    assert (directory / "main.py").is_file()
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    removed = run_cli(root, script, "remove", "demo")
    assert removed.returncode == 0, removed.stderr
    assert directory.exists()
    assert installed(site) == {}
    synced = run_cli(root, script, "sync")
    assert synced.returncode == 0, synced.stderr
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}


async def test_management_preserves_kernel_project_files_and_host_environment(
    deployment_cli,
):
    from importlib.metadata import distributions
    from pathlib import Path

    root, site, script = deployment_cli
    project_files = {
        "pyproject.toml": '[project]\nname="boundary-test"\nversion="1.0"\ndependencies=[]\n',
        "uv.lock": "project lock must remain unchanged\n",
    }
    for name, content in project_files.items():
        (root / name).write_text(content)
    kernel = Path(__file__).resolve().parents[3] / "src/alira/kernel"
    source_before = {path: path.read_bytes() for path in kernel.rglob("*.py")}
    assert source_before
    host_before = {dist.metadata["Name"]: dist.version for dist in distributions()}
    for command in [("add", "demo"), ("remove", "demo"), ("sync",)]:
        result = run_cli(root, script, *command)
        assert result.returncode == 0, result.stderr
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    assert {name: (root / name).read_text() for name in project_files} == project_files
    assert {path: path.read_bytes() for path in kernel.rglob("*.py")} == source_before
    assert {
        dist.metadata["Name"]: dist.version for dist in distributions()
    } == host_before


async def test_real_sync_no_install_and_corrupt_record_recovery(deployment_cli):
    root, site, script = deployment_cli
    resolved = run_cli(root, script, "sync", "--no-install")
    assert resolved.returncode == 0, resolved.stderr
    assert installed(site) == {}
    state = root / "state"
    assert (state / "modules.json").exists()
    assert (state / "pylock.toml").exists()
    (state / "modules.json").write_text("{broken")
    (state / "pylock.toml").write_text("broken lock")
    # The CLI must not restore records before dispatching sync.
    text = script.read_text().replace("module_manager._dependencies.restore()\n", "")
    script.write_text(text)
    repaired = run_cli(root, script, "sync")
    assert repaired.returncode == 0, repaired.stderr
    assert installed(site) == {"left-pkg": "1.0", "shared-pkg": "2.0"}
    assert set(json.loads((state / "modules.json").read_text())["modules"]) == {"demo"}
