"""Run the installed console script in its own interpreter, home and package tree."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
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
        key = hashlib.sha256(str(self.modules.resolve()).encode()).hexdigest()[:16]
        return (Path(self.env["HOME"]) / ".alira") / "projects" / key

    def records(self):
        return {
            path.name: path.read_bytes()
            for path in (self.state / "modules.json", self.state / "modules.lock.json")
            if path.exists()
        }

    def declare(self, name, requirements="", *, required_modules=()):
        folder = self.modules / name
        (folder / "src").mkdir(parents=True, exist_ok=True)
        (folder / "src/__init__.py").touch()
        (folder / "__init__.py").write_text("")
        (folder / "main.py").write_text(
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
        if key
        not in {
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "PYTHONHOME",
            "UV_PUBLISH_TOKEN",
            "ALIRA_MODULES_DIR",
        }
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
    modules = tmp_path / "src/alira/modules"
    modules.mkdir(parents=True)
    (modules / "__init__.py").touch()
    return Installation(
        tmp_path,
        python,
        scripts / ("alira.exe" if sys.platform == "win32" else "alira"),
        modules,
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


def package_manifest(app, requirements):
    """Archive dependent modules in reverse order; never execute their entries."""
    archives = app.root / "archives"
    archives.mkdir()
    for name, dependency, required in (
        ("child", "shared-pkg==1.0\n", ("base",)),
        ("base", "left-pkg==1.0\n", ()),
    ):
        folder = app.declare(name, requirements + dependency, required_modules=required)
        with ZipFile(archives / f"{name}.zip", "w") as package:
            for path in folder.rglob("*"):
                if path.is_file():
                    package.write(path, path.relative_to(folder))
        shutil.rmtree(folder)
    manifest = archives / "package.yml"
    manifest.write_text("modules:\n  - source: child.zip\n  - source: base.zip\n")
    return manifest


def test_manifest_add_locks_then_installs_and_reuses_sources(
    installation, local_requirements
):
    app = installation
    core = app.packages()
    manifest = package_manifest(app, local_requirements)
    app.run("add", "--manifest", manifest, "--no-sync")
    assert app.packages() == core
    declaration = json.loads(app.records()["modules.json"])
    assert declaration["format"] == 2
    assert set(declaration["modules"]) == {"base", "child"}
    assert "packages" not in declaration["modules"]["base"]
    lock = json.loads(app.records()["modules.lock.json"])
    assert set(lock["packages"]) == {"left-pkg", "shared-pkg"}
    assert all(item["sha256"] and item["wheels"] for item in lock["packages"].values())
    assert not (app.state / "pylock.toml").exists()
    before = tree_snapshot(app.modules)
    app.run("install", "--manifest", manifest)
    assert tree_snapshot(app.modules) == before
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    assert app.run("list").stdout.splitlines() == [
        "NAME   VERSION",
        "base   1",
        "child  1",
    ]
    records = app.records()
    app.run("lock", "--check", env={"PATH": str(app.root / "no-installer")})
    app.run("sync", "--locked")
    assert app.records() == records
    assert "child v1" in app.run("tree").stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fault injection")
def test_failed_install_retains_sources_and_packages_but_restores_state(
    installation, local_requirements
):
    app = installation
    core = app.packages()
    manifest = package_manifest(app, local_requirements)
    records = app.records()
    fault = uv_wrapper(
        app,
        "if arguments[:2] == ['pip', 'install']:\n"
        "    result = subprocess.run([real, *arguments])\n"
        "    sys.exit(result.returncode or 71)\n",
    )
    failure = app.run("add", "--manifest", manifest, env=fault, success=False)
    assert failure.returncode == 3
    assert app.records() == records
    assert (app.modules / "base/main.py").exists()
    assert (app.modules / "child/main.py").exists()
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    snapshot = tree_snapshot(app.modules)
    app.run("add", "--manifest", manifest)
    assert tree_snapshot(app.modules) == snapshot
    assert set(json.loads(app.records()["modules.json"])["modules"]) == {
        "base",
        "child",
    }


@pytest.mark.parametrize(
    "invalid", ["shared-pkg==2.0\n", "-r missing.txt\n", "-r 'unclosed\n"]
)
def test_resolution_failure_restores_state_and_keeps_published_source(
    installation, local_requirements, invalid
):
    app = installation
    app.declare("existing", local_requirements + "shared-pkg==1.0\n")
    app.run("add", "existing")
    before, records = app.packages(), app.records()
    folder = app.declare("bad", local_requirements + invalid)
    archive = app.root / "bad.zip"
    with ZipFile(archive, "w") as package:
        for path in folder.rglob("*"):
            if path.is_file():
                package.write(path, path.relative_to(folder))
    shutil.rmtree(folder)
    failure = app.run("add", archive, success=False)
    assert failure.returncode == 1
    assert folder.exists()
    assert app.packages() == before
    assert app.records() == records
    app.run("sync", "--locked")
    assert app.packages() == before


def test_remove_keeps_sources_and_sync_never_readds_them(
    installation, local_requirements
):
    app = installation
    core = app.packages()
    folder = app.declare("demo", local_requirements + "left-pkg==1.0\n")
    app.run("add", "demo")
    app.run("remove", "demo")
    assert folder.exists()
    assert app.packages() == core
    app.run("sync")
    assert app.packages() == core
    assert json.loads(app.records()["modules.json"])["modules"] == {}


def test_stale_locked_sync_and_lock_check_are_read_only(
    installation, local_requirements
):
    app = installation
    folder = app.declare("demo", local_requirements + "left-pkg==1.0\n")
    app.run("add", "demo", "--no-sync")
    (folder / "requirements.txt").write_text(local_requirements + "left-pkg==2.0\n")
    before = app.records()
    for arguments in [("sync", "--locked"), ("lock", "--check")]:
        failure = app.run(*arguments, success=False)
        assert failure.returncode == 1
        assert app.records() == before
    app.run("sync", "--frozen")
    assert app.packages()["left-pkg"] == "1.0"
    assert app.records() == before
    app.run("sync")
    assert app.packages()["left-pkg"] == "2.0"


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
    site = Path(
        app.python_run("import sysconfig; print(sysconfig.get_path('purelib'))")
    )
    path = site / "left_pkg-1.0.dist-info/METADATA"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    app.run("sync", "--locked")
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}


def test_installed_cli_requires_a_source_checkout_and_creates_no_state(
    installation, tmp_path_factory
):
    app = installation
    unrelated = tmp_path_factory.mktemp("without-source")
    for arguments in [
        ("sync",),
        ("lock",),
        ("add", "missing"),
        ("remove", "missing"),
        ("run",),
    ]:
        result = subprocess.run(
            [str(app.cli), *arguments],
            check=False,
            cwd=unrelated,
            env=app.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert (
            "alira must run from a source checkout or set ALIRA_MODULES_DIR"
            in result.stderr
        )
    assert not (Path(app.env["HOME"]) / ".alira").exists()


def test_checkouts_with_shared_home_keep_independent_declarations(installation):
    app = installation
    app.declare("same")
    app.run("add", "same", "--no-sync")
    first = app.records()
    second_root = app.root / "other-checkout"
    second_modules = second_root / "src/alira/modules"
    second_modules.mkdir(parents=True)
    original = app.modules
    app.modules = second_modules
    app.declare("same")
    (second_modules / "same/main.py").write_text(
        "from alira.kernel.module import BaseModule\nmodule=BaseModule('same','2')\n"
    )
    result = subprocess.run(
        [str(app.cli), "add", "same", "--no-sync"],
        check=False,
        cwd=second_root,
        env=app.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        json.loads(app.records()["modules.json"])["modules"]["same"]["version"] == "2"
    )
    assert app.state != (Path(app.env["HOME"]) / ".alira") / "modules"
    app.modules = original
    assert app.records() == first
    assert json.loads(first["modules.json"])["modules"]["same"]["version"] == "1"
    assert not list((Path(app.env["HOME"]) / ".alira").rglob("main.py"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGKILL process groups")
@pytest.mark.parametrize("phase", ["publish", "compile", "install"])
def test_force_kill_requires_repair_and_resumes_source_receipts(
    installation, local_requirements, phase
):
    app = installation
    core = app.packages()
    manifest = package_manifest(app, local_requirements)
    marker = app.root / "interrupted"
    if phase == "publish":
        injected = app.root / "fault-injection"
        injected.mkdir()
        (injected / "sitecustomize.py").write_text(
            "import os, time\nfrom pathlib import Path\noriginal=os.replace\n"
            "def replace(source, target):\n"
            "    result=original(source,target)\n"
            f"    if Path(target) == Path({str(app.modules / 'child')!r}):\n"
            f"        Path({str(marker)!r}).touch()\n"
            "        time.sleep(60)\n"
            "    return result\nos.replace=replace\n"
        )
        fault = {"PYTHONPATH": str(injected)}
    else:
        fault = uv_wrapper(
            app,
            f"if arguments[:2] == ['pip', {phase!r}]:\n"
            + (
                "    subprocess.run([real, *arguments], check=True)\n"
                if phase == "install"
                else ""
            )
            + f"    Path({str(marker)!r}).touch()\n    time.sleep(60)\n",
        )
    process = subprocess.Popen(
        [str(app.cli), "add", "--manifest", str(manifest)],
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
    assert (app.state / "journal.json").exists()
    # Force a mismatched pair, modelling termination between the atomic writes.
    (app.state / "modules.json").write_text("interrupted declaration")
    failure = app.run("sync", success=False)
    assert failure.returncode == 1 and "--repair" in failure.stderr
    app.run("sync", "--repair")
    assert json.loads(app.records()["modules.json"])["modules"] == {}
    app.run("add", "--manifest", manifest)
    assert app.packages() == core | {"left-pkg": "1.0", "shared-pkg": "1.0"}
    assert not (app.state / "journal.json").exists()


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
        app.env["ALIRA_MODULES_DIR"] = str(app.modules)
    normalized = obsolete_name.replace("-", "_")
    info = f"{normalized}-1.0.dist-info"
    archive = app.root / f"{normalized}-1.0-py3-none-any.whl"
    main = Path(app.python_run("import alira; print(alira.__path__[0])")) / "main.py"
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
        run("sync", "--repair")
    assert run("--help").stdout.startswith("usage: alira")
    # Exercise the same project runner again: it must not need a reinstall.
    assert run("list").stdout.strip() == "NAME  VERSION"
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
        run("add", "demo", "--no-sync")
        process = subprocess.Popen(
            [app.uv, "run", "--project", str(app.root), "alira", "run", "demo"],
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


def test_installed_cli_init_creates_a_deployable_module(installation):
    app = installation
    packages, records = app.packages(), app.records()
    initialized = app.run("init", "sample_module")
    directory = app.modules / "sample_module"
    assert initialized.stdout.strip() == f"Initialized module: {directory}"
    assert (directory / "src/shared/events.py").is_file()
    assert (directory / "requirements.txt").read_text() == ""
    assert app.packages() == packages
    assert app.records() == records
    original = tree_snapshot(directory)
    duplicate = app.run("init", "sample_module", success=False)
    assert "already exists" in duplicate.stderr
    assert tree_snapshot(directory) == original

    app.run("add", "sample_module")
    app.python_run(
        "import asyncio\n"
        "from alira.kernel.module import module_manager\n"
        "async def verify():\n"
        "    await module_manager.start()\n"
        "    await module_manager.load('sample_module')\n"
        "    assert module_manager.loaded == {'sample_module'}\n"
        "    assert (await module_manager.check_health())['sample_module']['health'] is False\n"
        "    await module_manager.stop()\n"
        "    assert not module_manager.loaded\n"
        "asyncio.run(verify())\n"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process contention")
def test_concurrent_cli_times_out_with_owner_and_preserves_state(
    installation, local_requirements
):
    app = installation
    app.declare("demo", local_requirements + "left-pkg==1.0\n")
    marker = app.root / "resolving"
    fault = uv_wrapper(
        app,
        "if arguments[:2] == ['pip', 'compile']:\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(60)\n",
    )
    process = subprocess.Popen(
        [str(app.cli), "add", "demo"],
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
        before = app.records()
        failure = app.run("add", "demo", success=False)
        assert failure.returncode == 2
        assert "another alira command is using this modules directory" in failure.stderr
        assert f"pid {process.pid}" in failure.stderr
        assert "operation: add" in failure.stderr
        assert app.records() == before
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    app.run("add", "demo")
    assert app.packages()["left-pkg"] == "1.0"


def test_explicit_modules_root_works_outside_checkout(installation, tmp_path_factory):
    app = installation
    app.declare("explicit")
    unrelated = tmp_path_factory.mktemp("explicit-modules-root")
    result = subprocess.run(
        [str(app.cli), "add", "explicit", "--no-sync"],
        cwd=unrelated,
        env=app.env | {"ALIRA_MODULES_DIR": str(app.modules)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(json.loads(app.records()["modules.json"])["modules"]) == {"explicit"}


def test_migrate_preserves_locked_artifacts_without_running_resolver(
    installation, local_requirements
):
    from alira.kernel.module.dependencies.requirements import requirements_fingerprint
    from alira.kernel.module.dependencies.uv import installation_lock

    app = installation
    app.declare("demo", local_requirements + "left-pkg==1.0\n")
    app.run("add", "demo", "--no-sync")
    lock = json.loads(app.records()["modules.lock.json"])
    artifact = installation_lock(lock)
    legacy = json.dumps(
        {
            "format": 1,
            "lock_hash": hashlib.sha256(artifact).hexdigest(),
            "modules": {
                "demo": {
                    "packages": {
                        name: item["version"] for name, item in lock["packages"].items()
                    },
                    "required_modules": [],
                    "requirements": str(app.modules / "demo/requirements.txt"),
                    "requirements_hash": requirements_fingerprint(
                        app.modules / "demo/requirements.txt"
                    ),
                }
            },
        }
    ).encode()
    shutil.rmtree(app.state)
    home = Path(app.env["HOME"]) / ".alira"
    (home / "modules.json").write_bytes(legacy)
    (home / "pylock.toml").write_bytes(artifact)
    app.run("migrate", env={"PATH": str(app.root / "no-resolver")})
    migrated = json.loads(app.records()["modules.lock.json"])
    assert migrated["packages"] == lock["packages"]
    assert (home / "modules.json.v1.bak").read_bytes() == legacy
    assert (home / "pylock.toml.v1.bak").read_bytes() == artifact
    app.run("sync", "--locked")
    assert app.packages()["left-pkg"] == "1.0"
