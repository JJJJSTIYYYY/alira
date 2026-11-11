import importlib
import socket
import subprocess
import sys

import pytest
from httpx import AsyncClient

from alira.modules.web.main import WebModule


def test_declaration_does_not_import_web_business_packages():
    subprocess.run(
        [sys.executable, "-c", (
            "import sys\n"
            "import alira.modules.web.main\n"
            "assert 'alira.modules.web.src.runtime' not in sys.modules\n"
            "assert 'alira.modules.web.src.application' not in sys.modules\n"
        )],
        check=True, capture_output=True,
    )


async def test_web_module_serves_health_and_restarts_with_core(monkeypatch, core_lifecycle, module_manager_factory, kernel_lifecycle):
    runtime = importlib.import_module("alira.modules.web.src.runtime")
    monkeypatch.setattr(runtime, "BASE_URL", "http://127.0.0.1:0")
    manager = module_manager_factory()
    core_lifecycle.register(manager)
    # Requirements are already provided by the test environment. Package
    # installation is exercised against a separate environment in kernel tests.
    monkeypatch.setattr(manager._registry, "requirements_path", lambda name: None)
    for _ in range(2):
        async with kernel_lifecycle.lifespan():
            assert not manager.loaded
            await manager.load("web")
            assert manager.loaded == {"web"}
            web = manager.modules["web"]._runtime
            serving = web.task
            port = web.server.servers[0].sockets[0].getsockname()[1]
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "service": "alira"}
        assert serving.done()
        assert not manager.loaded
        assert not manager.dependencies


async def test_failed_web_bind_does_not_leave_resources_or_package_claims(tmp_path, monkeypatch, module_manager_factory):
    runtime = importlib.import_module("alira.modules.web.src.runtime")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(runtime, "BASE_URL", f"http://127.0.0.1:{port}")
        manager = module_manager_factory(tmp_path)
        module = WebModule()
        manager.register(module)
        with pytest.raises(ExceptionGroup) as caught:
            await manager.load()
        assert "Web server startup failed" in str(caught.value.exceptions[0])
        assert module._runtime is None
        assert not manager.loaded
        assert not manager.dependencies
        await manager.stop()


@pytest.mark.parametrize("exit_signal", ["SIGINT", "SIGTERM"])
async def test_process_signals_stop_web_before_core(tmp_path, exit_signal):
    import asyncio
    import signal

    from httpx import ConnectError

    data_dir = tmp_path / "data"
    (tmp_path / "config.yaml").write_text(f"SERVER:\n  base_dir: {data_dir}\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = (
        "from alira import main\n"
        "from alira.modules.web.src import runtime\n"
        f"runtime.BASE_URL = 'http://127.0.0.1:{port}'\n"
        "from alira.kernel.module import module_manager\n"
        "module_manager._registry.requirements_path = lambda name: None\n"
        "main.main(['--modules', 'web'])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], cwd=tmp_path,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with asyncio.timeout(5):
                while True:
                    if process.poll() is not None:
                        out, err = process.communicate()
                        pytest.fail(f"Server exited before startup: {out}\n{err}")
                    try:
                        response = await client.get("/health")
                    except ConnectError:
                        await asyncio.sleep(0.01)
                    else:
                        assert response.status_code == 200
                        break
        process.send_signal(getattr(signal, exit_signal))
        out, err = await asyncio.to_thread(process.communicate, timeout=5)
        assert process.returncode == 0, err
        assert out.index("Finished server process") < out.index("Stopping: ApixisCore")
        assert "Traceback" not in err
        logs = "".join(path.read_text() for path in data_dir.glob("logs/ALIRA/*.log"))
        assert "Finished server process" in logs
        assert "Stopping: Logger" in logs
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.communicate, timeout=5)


def hanging_runtime(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    runtime = importlib.import_module("alira.modules.web.src.runtime")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            cleaned.set()

    monkeypatch.setattr(runtime, "BASE_URL", "http://127.0.0.1:0")
    monkeypatch.setattr(runtime, "STARTUP_TIMEOUT", 0.02)
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.2)
    monkeypatch.setattr(runtime, "create_app", lambda: FastAPI(lifespan=lifespan))
    return runtime.WebRuntime(), entered, cleaned


async def test_hanging_web_startup_times_out_and_reaps_lifespan(monkeypatch):
    import asyncio

    web, entered, cleaned = hanging_runtime(monkeypatch)
    baseline = asyncio.all_tasks()
    async with asyncio.timeout(1):
        with pytest.raises(RuntimeError, match="startup timed out after 0.02 seconds"):
            await web.start()
    assert entered.is_set()
    assert cleaned.is_set()
    assert web.task is None
    assert web._lifespan_task is None
    assert not (asyncio.all_tasks() - baseline)
    await web.stop()


async def test_cancelled_web_startup_reaps_lifespan(monkeypatch):
    import asyncio

    runtime = importlib.import_module("alira.modules.web.src.runtime")
    web, entered, cleaned = hanging_runtime(monkeypatch)
    monkeypatch.setattr(runtime, "STARTUP_TIMEOUT", 1)
    baseline = asyncio.all_tasks()
    starting = asyncio.create_task(web.start())
    await entered.wait()
    starting.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await starting
    assert cleaned.is_set()
    assert web.task is None
    assert web._lifespan_task is None
    assert not (asyncio.all_tasks() - baseline)


async def test_hanging_web_shutdown_times_out_and_reaps_lifespan(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    runtime = importlib.import_module("alira.modules.web.src.runtime")
    shutting_down, cleaned = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            shutting_down.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    monkeypatch.setattr(runtime, "BASE_URL", "http://127.0.0.1:0")
    monkeypatch.setattr(runtime, "STARTUP_TIMEOUT", 1)
    monkeypatch.setattr(runtime, "SHUTDOWN_TIMEOUT", 0.2)
    monkeypatch.setattr(runtime, "create_app", lambda: FastAPI(lifespan=lifespan))
    web = runtime.WebRuntime()
    baseline = asyncio.all_tasks()
    await web.start()
    serving = web.task
    async with asyncio.timeout(1):
        with pytest.raises(RuntimeError, match="shutdown timed out after 0.2 seconds"):
            await web.stop()
    assert shutting_down.is_set()
    assert cleaned.is_set()
    assert serving.done()
    assert web.task is None
    assert web._lifespan_task is None
    assert all(not server.is_serving() for server in web.server.servers)
    assert not (asyncio.all_tasks() - baseline)
