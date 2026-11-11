"""Run the installed console script in its own interpreter, home and package tree."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest

from alira.kernel.module.dependencies.deployment import kernel_packages
from tests.kernel.module.dependencies.test_uv import wheel


@pytest.fixture(scope="module")
def isolated_wheel(tmp_path_factory):
    """Build actual current package files without writing into the checkout."""
    project = Path(__file__).resolve().parents[3]
    staging = tmp_path_factory.mktemp("alira-distribution")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(project / name, staging / name)
    shutil.copytree(
        project / "src/alira",
        staging / "src/alira",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "config.yaml"),
    )
    modules = staging / "src/alira/modules"
    shutil.rmtree(modules)
    modules.mkdir()
    (modules / "__init__.py").touch()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_wheel; build_wheel('dist')",
        ],
        cwd=staging,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return next((staging / "dist").glob("*.whl"))


@dataclass
class Installation:
    root: Path
    python: Path
    cli: Path
    modules: Path
    env: dict[str, str] = field(repr=False)
    uv: str

    def run(self, *arguments, env=None, success=True):
        result = subprocess.run(
            [str(self.cli), *map(str, arguments)],
            cwd=self.root,
            env=self.env | (env or {}),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert (result.returncode == 0) is success, result.stdout + result.stderr
        assert "Registered service:" not in result.stdout + result.stderr
        assert "Started:" not in result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        return result

    def python_run(self, code):
        result = subprocess.run(
            [str(self.python), "-c", code],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    def packages(self):
        return json.loads(
            self.python_run(
                "import json; from importlib.metadata import distributions; "
                "from packaging.utils import canonicalize_name; "
                "print(json.dumps({canonicalize_name(d.metadata['Name']): d.version "
                "for d in distributions()}))"
            )
        )

    @property
    def state(self):
        return Path(self.env["HOME"]) / ".alira"

    def records(self):
        return {
            path.name: path.read_bytes()
            for path in (self.state / "modules.json", self.state / "pylock.toml")
            if path.exists()
        }

    def declare(self, name, requirements="", *, required_modules=()):
        folder = self.modules / name
        (folder / "src").mkdir(parents=True, exist_ok=True)
        (folder / "src/__init__.py").touch()
        (folder / "__init__.py").write_text("raise AssertionError('entry imported')\n")
        (folder / "main.py").write_text(
            "raise AssertionError('entry executed')\n"
            "from alira.kernel.module import BaseModule\n"
            f"module=BaseModule({name!r}, '1', required_modules={list(required_modules)!r})\n"
        )
        (folder / "requirements.txt").write_text(requirements)
        return folder


@pytest.fixture
def installation(tmp_path, isolated_wheel):
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for isolated CLI tests"
    environment = tmp_path / "environment"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    constraints = tmp_path / "core.txt"
    constraints.write_text(
        "".join(f"{name}=={version}\n" for name, version in kernel_packages().items())
    )
    home = tmp_path / "home"
    home.mkdir()
    cache = subprocess.run(
        [uv, "cache", "dir"], check=True, capture_output=True, text=True
    ).stdout.strip()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PUBLISH_TOKEN"}
    } | {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "UV_OFFLINE": "1",
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": cache,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraints),
            str(isolated_wheel),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    module_path = subprocess.run(
        [str(python), "-c", "import alira; print(alira.__path__[0])"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Installation(
        tmp_path,
        python,
        scripts / ("alira.exe" if sys.platform == "win32" else "alira"),
        Path(module_path) / "modules",
        env,
        uv,
    )


def tree_snapshot(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


@pytest.fixture
def local_requirements(installation):
    wheels = installation.root / "wheels"
    wheels.mkdir()
    wheel(wheels, "shared-pkg", "1.0")
    wheel(wheels, "shared-pkg", "2.0")
    wheel(wheels, "left-pkg", "1.0", ["shared-pkg<2"])
    wheel(wheels, "left-pkg", "2.0", ["shared-pkg>=2"])
    wheel(wheels, "orphan-pkg", "1.0")
    return f"--no-index\n--find-links {wheels}\n"


def uv_wrapper(app, body):
    scripts = app.root / "fault-bin"
    scripts.mkdir()
    wrapper = scripts / "uv"
    wrapper.write_text(
        f"#!{app.python}\n"
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"real = {app.uv!r}\narguments = sys.argv[1:]\n"
        + body
        + "os.execv(real, [real, *arguments])\n"
    )
    wrapper.chmod(0o755)
    return {"PATH": str(scripts) + os.pathsep + app.env["PATH"]}


@pytest.mark.parametrize("obsolete_name", ["alira", "other-console-owner"])
@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize(
    "interrupted",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="POSIX SIGKILL fault injection"
            ),
        ),
    ],
)
def test_uv_run_sync_preserves_kernel_files_shared_with_removed_distribution(
    installation, isolated_wheel, obsolete_name, editable, interrupted
):
    app = installation
    if editable:
        project = app.root / "editable-project"
        project.mkdir()
        staging = isolated_wheel.parent.parent
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(staging / name, project / name)
        shutil.copytree(staging / "src/alira", project / "src/alira")
        subprocess.run(
            [
                app.uv,
                "pip",
                "install",
                "--python",
                str(app.python),
                "--no-deps",
                "--reinstall",
                "--editable",
                str(project),
            ],
            env=app.env,
            check=True,
            capture_output=True,
        )
        app.modules = (
            Path(app.python_run("import alira; print(alira.__path__[0])")) / "modules"
        )
    normalized = obsolete_name.replace("-", "_")
    info = f"{normalized}-1.0.dist-info"
    archive = app.root / f"{normalized}-1.0-py3-none-any.whl"
    main = app.modules.parent / "main.py"
    original_main = main.read_bytes()
    with ZipFile(archive, "w") as package:
        package.writestr(
            f"{info}/METADATA",
            f"Metadata-Version: 2.3\nName: {obsolete_name}\nVersion: 1.0\n",
        )
        package.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        package.writestr(
            f"{info}/entry_points.txt", "[console_scripts]\nalira = alira.main:main\n"
        )
        package.writestr("alira/main.py", original_main)
        package.writestr(
            f"{info}/RECORD",
            "".join(f"{name},,\n" for name in [*package.namelist(), f"{info}/RECORD"]),
        )
    subprocess.run(
        [
            app.uv,
            "pip",
            "install",
            "--python",
            str(app.python),
            "--no-deps",
            str(archive),
        ],
        env=app.env,
        check=True,
        capture_output=True,
    )
    (app.root / "pyproject.toml").write_text(
        '[project]\nname="launcher-check"\nversion="1.0"\ndependencies=['
        + json.dumps(
            "alira-harness"
            if editable
            else "alira-harness @ " + isolated_wheel.as_uri()
        )
        + "]\n"
        + (
            '[tool.uv.sources]\nalira-harness={path="editable-project",editable=true}\n'
            if editable
            else ""
        )
    )
    env = app.env | {"UV_PROJECT_ENVIRONMENT": str(app.python.parent.parent)}

    def run(*arguments):
        result = subprocess.run(
            [app.uv, "run", "--project", str(app.root), "alira", *arguments],
            cwd=app.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    if interrupted:
        marker = app.root / "uninstalled"
        fault = uv_wrapper(
            app,
            "if arguments[:2] == ['pip', 'uninstall']:\n"
            "    subprocess.run([real, *arguments], check=True)\n"
            f"    Path({str(marker)!r}).touch()\n"
            "    time.sleep(60)\n",
        )
        process = subprocess.Popen(
            [app.uv, "run", "--project", str(app.root), "alira", "sync"],
            cwd=app.root,
            env=env | fault,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not marker.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert marker.exists()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
    else:
        run("sync")
    assert obsolete_name not in app.packages()
    assert app.cli.is_file()
    assert main.read_bytes() == original_main
    if interrupted:
        run("sync")
    assert run("--help").stdout.startswith("usage: alira")
    # Exercise the same project runner again: it must not need a reinstall.
    assert run("list").stdout.strip() == "NAME"
    if sys.platform != "win32":
        ready = app.root / "ready"
        folder = app.declare("demo")
        (folder / "requirements.txt").unlink()
        (folder / "__init__.py").write_text("")
        (folder / "main.py").write_text(
            "from pathlib import Path\nfrom alira.kernel.module import BaseModule\n"
            "class Demo(BaseModule):\n"
            "    async def startup(self):\n"
            f"        Path({str(ready)!r}).touch()\n"
            "module=Demo('demo','1')\n"
        )
        process = subprocess.Popen(
            [app.uv, "run", "--project", str(app.root), "alira", "--modules", "demo"],
            cwd=app.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert ready.exists()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr


def test_installed_cli_sync_add_remove_use_own_interpreter_and_home(
    installation, local_requirements
):
    app = installation
    core = app.packages()
    source = tree_snapshot(app.modules.parent)
    project = Path(__file__).resolve().parents[3]
    project_files = {
        name: (project / name).read_bytes() for name in ("pyproject.toml", "uv.lock")
    }
    base = app.declare("base", local_requirements + "shared-pkg<3\n")
    demo = app.declare(
        "demo", local_requirements + "left-pkg==1.0\n", required_modules=["base"]
    )
    unmanaged = app.declare("unmanaged", "nonexistent-package==0\n")
    (unmanaged / "main.py").write_text("invalid syntax !!!")
    assert app.run("list").stdout.splitlines() == ["NAME", "base", "demo", "unmanaged"]
    app.run("--help")
    assert app.records() == {}
    assert app.packages() == core
    app.run("add", "demo", "--no-install")
    assert app.packages() == core
    records = json.loads(app.records()["modules.json"])["modules"]
    assert set(records) == {"base", "demo"}
    assert records["demo"]["required_modules"] == ["base"]
    assert (
        "wheels" in tomllib.loads(app.records()["pylock.toml"].decode())["packages"][0]
    )
    app.run("add", "demo")
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    (demo / "requirements.txt").write_text(local_requirements + "left-pkg==2.0\n")
    app.run("add", "demo")
    assert app.packages() == core | {"left-pkg": "2.0", "shared-pkg": "2.0"}
    before = app.records()
    app.run("add", "missing", success=False)
    app.run("remove", "missing", success=False)
    app.run("remove", "base", success=False)
    assert app.records() == before
    app.run("remove", "demo")
    assert app.packages() == core | {"shared-pkg": "2.0"}
    assert (demo / "main.py").is_file()
    app.run("remove", "base")
    assert app.packages() == core
    shutil.rmtree(unmanaged)
    app.run("sync", "--no-install")
    assert app.packages() == core
    app.run("sync")
    assert app.packages() == core | {"left-pkg": "2.0", "shared-pkg": "2.0"}
    subprocess.run(
        [
            app.uv,
            "pip",
            "install",
            "--python",
            str(app.python),
            str(app.root / "wheels/orphan_pkg-1.0-py3-none-any.whl"),
        ],
        env=app.env,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(demo)
    (base / "requirements.txt").write_text(local_requirements + "shared-pkg==1.0\n")
    (app.state / "modules.json").write_text("{broken")
    (app.state / "pylock.toml").unlink()
    app.run("sync")
    assert app.packages() == core | {"shared-pkg": "1.0"}
    assert set(json.loads(app.records()["modules.json"])["modules"]) == {"base"}
    after = tree_snapshot(app.modules.parent)
    assert {name: after[name] for name in source} == source
    assert {
        name: (project / name).read_bytes() for name in project_files
    } == project_files


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fault injection")
def test_failed_install_restores_original_locked_artifacts(
    installation, local_requirements
):
    app = installation
    wheels = app.root / "wheels"
    newer = wheels / "left_pkg-2.0-py3-none-any.whl"
    saved = app.root / newer.name
    newer.rename(saved)
    demo = app.declare("demo", local_requirements + "left-pkg<3\n")
    app.run("add", "demo")
    before = app.packages()
    records = app.records()
    source = tree_snapshot(demo)
    saved.rename(newer)
    folder = app.declare(
        "plugin", local_requirements + "left-pkg==2.0\norphan-pkg==1.0\n"
    )
    archive = app.root / "plugin.zip"
    with ZipFile(archive, "w") as package:
        for path in folder.rglob("*"):
            if path.is_file():
                package.write(path, path.relative_to(folder))
    shutil.rmtree(folder)
    failed = app.root / "installer-failed"
    fault = uv_wrapper(
        app,
        f"marker = Path({str(failed)!r})\n"
        "if arguments[:2] == ['pip', 'install'] and not marker.exists():\n"
        "    result = subprocess.run([real, *arguments])\n"
        "    if result.returncode: sys.exit(result.returncode)\n"
        "    marker.touch()\n"
        "    sys.exit(71)\n",
    )
    failure = app.run(
        "install",
        archive,
        env=fault,
        success=False,
    )
    assert failed.exists()
    assert "code 71" in failure.stderr
    assert "rollback was incomplete" not in failure.stderr
    assert not folder.exists()
    assert app.records() == records
    assert app.packages() == before
    assert tree_snapshot(demo) == source


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fault injection")
def test_uninstall_dependency_failure_rolls_back_without_touching_sources(
    installation, local_requirements
):
    app = installation
    folder = app.declare("demo", local_requirements + "left-pkg==1.0\n")
    app.run("add", "demo")
    before = app.packages()
    records = app.records()
    source = tree_snapshot(folder)
    fault = uv_wrapper(
        app,
        "if arguments[:2] == ['pip', 'uninstall']:\n"
        "    result = subprocess.run([real, *arguments])\n"
        "    sys.exit(result.returncode or 72)\n",
    )
    failure = app.run("uninstall", "demo", env=fault, success=False)
    assert "code 72" in failure.stderr
    assert "rollback was incomplete" not in failure.stderr
    assert app.packages() == before
    assert app.records() == records
    assert tree_snapshot(folder) == source


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "Metadata-Version: 2.3\nName: left-pkg\n",
        "Metadata-Version: 2.3\nName: left-pkg\nVersion: 0\n",
        "Metadata-Version: 2.3\nName: left-pkg\nVersion: incomplete\n",
    ],
)
def test_sync_repairs_incomplete_distribution_metadata(
    installation, local_requirements, metadata
):
    app = installation
    core = app.packages()
    app.declare("demo", local_requirements + "left-pkg==1.0\n")
    app.run("add", "demo")
    # Model the on-disk fragment left by an interrupted package installation.
    path = app.modules.parent.parent / "left_pkg-1.0.dist-info/METADATA"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    app.run("sync")
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}


def test_empty_requirements_generate_reusable_records(installation):
    app = installation
    core = app.packages()
    folder = app.declare("empty")
    app.run("add", "empty", "--no-install")
    app.run("add", "empty")
    app.run("sync")
    app.run("remove", "empty")
    assert (folder / "main.py").is_file()
    assert app.packages() == core
    assert json.loads(app.records()["modules.json"])["modules"] == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL process groups")
@pytest.mark.parametrize("phase", ["compile", "install", "uninstall"])
def test_sync_repairs_force_killed_resolution_or_partial_install(
    installation, local_requirements, phase
):
    app = installation
    core = app.packages()
    demo = app.declare("demo", local_requirements + "left-pkg==1.0\n")
    source = tree_snapshot(demo)
    if phase == "uninstall":
        app.run("add", "demo")
    marker = app.root / "interrupted"
    fault = uv_wrapper(
        app,
        f"if arguments[:2] == ['pip', {phase!r}]:\n"
        + (
            f"    subprocess.run([real, 'pip', 'install', '--python', {str(app.python)!r}, "
            f"'--no-deps', {str(app.root / 'wheels/left_pkg-1.0-py3-none-any.whl')!r}], check=True)\n"
            if phase == "install"
            else f"    subprocess.run([real, 'pip', 'uninstall', '--python', {str(app.python)!r}, 'left-pkg'], check=True)\n"
            if phase == "uninstall"
            else ""
        )
        + f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(60)\n",
    )
    process = subprocess.Popen(
        [str(app.cli), "remove" if phase == "uninstall" else "add", "demo"],
        cwd=app.root,
        env=app.env | fault,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not marker.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert marker.exists(), process.communicate(timeout=1)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL
    partial = {"install": {"left-pkg": "1.0"}, "uninstall": {"shared-pkg": "1.0"}}
    assert app.packages() == core | partial.get(phase, {})
    app.run("sync")
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    assert tree_snapshot(demo) == source
    assert set(json.loads(app.records()["modules.json"])["modules"]) == {"demo"}


@pytest.mark.skipif(
    os.environ.get("ALIRA_TEST_NETWORK") != "1", reason="set ALIRA_TEST_NETWORK=1"
)
def test_real_pypi_transformers_and_github_validation(installation):
    app = installation
    online = {"UV_OFFLINE": "0", "UV_DEFAULT_INDEX": "https://pypi.org/simple"}
    core = app.packages()
    demo = app.declare("transformer_sample", "transformers==4.57.1\n")
    app.run("add", "transformer_sample", "--no-install", env=online)
    assert app.packages() == core

    app.run("add", "transformer_sample", env=online)
    assert app.packages()["transformers"] == "4.57.1"
    assert all(app.packages()[name] == version for name, version in core.items())
    app.python_run("import transformers; assert transformers.__version__ == '4.57.1'")
    snapshot = app.records()
    packages = app.packages()
    failure = app.run(
        "install",
        "https://github.com/JJJJSTIYYYY/apixis-example",
        "--ref",
        "main",
        env=online,
        success=False,
    )
    assert "must contain main.py" in failure.stderr
    assert app.records() == snapshot
    assert app.packages() == packages
    assert app.run("list").stdout.splitlines() == ["NAME", "transformer_sample"]
    app.run("remove", "transformer_sample", env=online)
    assert (demo / "main.py").exists()
    assert app.packages() == core


@pytest.mark.skipif(
    os.environ.get("ALIRA_TEST_NETWORK") != "1", reason="set ALIRA_TEST_NETWORK=1"
)
def test_sync_keeps_uv_when_it_is_installed_in_the_alira_interpreter(installation):
    app = installation
    online = {"UV_OFFLINE": "0", "UV_DEFAULT_INDEX": "https://pypi.org/simple"}
    result = subprocess.run(
        [app.uv, "pip", "install", "--python", str(app.python), "uv==0.9.7"],
        env=app.env | online,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    baseline = app.packages()
    own_tool = {"PATH": str(app.cli.parent)}
    app.run("sync", env=own_tool)
    assert app.packages() == baseline
    app.declare("empty")
    app.run("add", "empty", env=own_tool)


@pytest.mark.skipif(
    os.environ.get("ALIRA_TEST_SYSTEM_TRASH") != "1",
    reason="set ALIRA_TEST_SYSTEM_TRASH=1 to exercise the real OS trash",
)
def test_uninstall_reaches_real_system_trash(installation, local_requirements):
    app = installation
    name = "alira_trash_test_" + uuid4().hex
    folder = app.declare(name, local_requirements + "left-pkg==1.0\n")
    source = tree_snapshot(folder)
    archive = app.root / "plugin.zip"
    with ZipFile(archive, "w") as package:
        for path in folder.rglob("*"):
            if path.is_file():
                package.write(path, path.relative_to(folder))
    shutil.rmtree(folder)
    core = app.packages()
    app.run("install", archive)
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    app.run("uninstall", name)
    assert not folder.exists()
    assert app.packages() == core
    assert json.loads(app.records()["modules.json"])["modules"] == {}
    # macOS's native Send2Trash backend uses the logged-in user's actual trash,
    # regardless of the isolated HOME used by Alira's records.
    if sys.platform == "darwin":
        import pwd

        trash = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".Trash"
        assert tree_snapshot(trash / name) == source
