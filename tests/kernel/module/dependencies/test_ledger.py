"""Declarations, locks and interrupted operations preserve project-state ownership."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alira.kernel.module.dependencies.ledger import DependencyLedger, inputs_hash
from alira.kernel.module.dependencies.types import DependencyError, DependencyLockError


def declaration():
    return {
        "format": 2,
        "requires-kernel": ">=3,<4",
        "modules": {
            "demo": {
                "version": "1.0",
                "source": {"type": "builtin"},
                "required-modules": [],
                "requirements": None,
                "requirements-hash": None,
            }
        },
    }


def lock():
    return {
        "format": 1,
        "generated-by": "alira 3.0.1",
        "inputs-hash": "sha256:example",
        "kernel": {"version": "3.0.1", "python": "3.12.12"},
        "modules": {
            "demo": {
                "version": "1.0",
                "source": {"type": "builtin"},
                "source-sha256": "a" * 64,
                "required-modules": [],
                "requirements-hash": None,
                "packages": [],
            }
        },
        "packages": {},
    }


def package():
    return {
        "name": "demo-pkg",
        "version": "1.0",
        "sha256": "b" * 64,
        "wheels": [
            {
                "url": "https://example.org/demo_pkg-1.0-py3-none-any.whl",
                "hashes": {"sha256": "c" * 64},
            }
        ],
    }


def test_missing_state_reads_without_creating_directories(tmp_path):
    ledger = DependencyLedger(tmp_path / "absent")
    assert ledger.read()["modules"] == {}
    assert ledger.read()["format"] == 2
    assert ledger.read_lock() is None
    assert not ledger.directory.exists()


def test_declaration_and_lock_are_independent(tmp_path):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(declaration())
    assert ledger.read() == declaration()
    assert ledger.read_lock() is None
    ledger.write_lock(lock())
    assert ledger.read_lock() == lock()
    ledger.path.unlink()
    assert ledger.read()["modules"] == {}
    assert ledger.read_lock() == lock()


@pytest.mark.parametrize(
    "data",
    [
        {"format": 1, "modules": {}},
        {"format": 2, "requires-kernel": "", "modules": []},
        {"format": 2, "requires-kernel": "bad constraint", "modules": {}},
    ],
)
def test_invalid_declarations_have_actionable_error(tmp_path, data):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(data)
    with pytest.raises(DependencyError, match="repair modules.json"):
        ledger.read()


def test_locked_package_ownership_is_validated(tmp_path):
    ledger = DependencyLedger(tmp_path)
    data = lock()
    data["packages"]["demo-pkg"] = package()
    ledger.write_lock(data)
    with pytest.raises(DependencyError, match="alira lock"):
        ledger.read_lock()


@pytest.mark.parametrize("location", ["wheel-url", "wheel-path", "sdist"])
def test_lock_preserves_supported_hashed_artifact_locations(tmp_path, location):
    ledger = DependencyLedger(tmp_path)
    data = lock()
    locked = package()
    if location == "wheel-path":
        artifact = locked["wheels"][0]
        artifact.pop("url")
        artifact["path"] = "/tmp/demo_pkg-1.0-py3-none-any.whl"
    elif location == "sdist":
        locked["sdist"] = locked.pop("wheels")[0]
        locked["sdist"]["url"] = "https://example.org/demo_pkg-1.0.tar.gz"
    data["packages"]["demo-pkg"] = locked
    data["modules"]["demo"]["packages"] = ["demo-pkg"]
    ledger.write_lock(data)
    assert ledger.read_lock() == data


@pytest.mark.parametrize(
    "path,value",
    [
        (("packages", "demo-pkg", "name"), None),
        (("packages", "demo-pkg", "name"), "another-pkg"),
        (("packages", "demo-pkg", "sha256"), "invalid"),
        (("packages", "demo-pkg", "sha256"), 1),
        (("packages", "demo-pkg", "version"), []),
        (("packages", "demo-pkg", "wheels"), {}),
        (("packages", "demo-pkg", "wheels"), []),
        (("packages", "demo-pkg", "wheels"), [None]),
        (("packages", "demo-pkg", "wheels"), [{"url": "file:///demo.whl"}]),
        (("packages", "demo-pkg", "wheels"), [{"hashes": {"sha256": "a" * 64}}]),
        (
            ("packages", "demo-pkg", "wheels"),
            [{"url": 3, "hashes": {"sha256": "a" * 64}}],
        ),
        (("packages", "demo-pkg", "sdist"), []),
        (("modules", "demo", "source"), None),
        (("modules", "demo", "source"), {"type": "path"}),
        (("modules", "demo", "source"), {"type": "git", "url": 3}),
        (("modules", "demo", "required-modules"), None),
        (("modules", "demo", "required-modules"), [3]),
        (("modules", "demo", "requirements-hash"), 3),
        (("modules", "demo", "requirements-hash"), None),
        (("modules", "demo", "requirements-hash"), "sha256:invalid"),
        (("modules", "demo", "source-sha256"), "invalid"),
        (("modules", "demo", "packages"), [[]]),
        (("install-metadata",), []),
    ],
)
def test_malformed_lock_metadata_produces_clean_cli_error(
    tmp_path, monkeypatch, module_manager_factory, capsys, path, value
):
    from alira.cli import entrypoint

    modules = tmp_path / "modules"
    modules.mkdir()
    manager = module_manager_factory(modules)
    ledger = manager._dependencies.ledger
    data = lock()
    data["packages"]["demo-pkg"] = package()
    data["modules"]["demo"]["packages"] = ["demo-pkg"]
    target = data
    for key in path[:-1]:
        target = target[key]
    if value is None:
        target.pop(path[-1], None)
    else:
        target[path[-1]] = value
    ledger.write_lock(data)
    monkeypatch.setattr(entrypoint, "module_manager", manager)
    before = ledger.snapshot()
    with pytest.raises(SystemExit) as caught:
        entrypoint.main(["sync", "--frozen"])
    assert caught.value.code == 1
    error = capsys.readouterr().err
    assert error == "Invalid module lock; run alira lock to regenerate it.\n"
    assert ledger.snapshot() == before


def test_inputs_hash_normalizes_json_and_tracks_resolver_identity():
    original = declaration()
    reordered = dict(reversed(tuple(original.items())))
    expected = inputs_hash(original, resolver_version="uv 0.9")
    assert inputs_hash(reordered, resolver_version="uv 0.9") == expected
    assert inputs_hash(original, resolver_version="uv 0.10") != expected
    assert (
        inputs_hash(original, resolver_version="uv 0.9", python_version="3.12.1")
        != expected
    )


async def test_transaction_success_records_sources_and_removes_journal(tmp_path):
    ledger = DependencyLedger(tmp_path)
    async with ledger.transaction("add"):
        ledger.write_declaration(declaration())
        ledger.write_lock(lock())
        ledger.record_source("demo")
        journal = json.loads(ledger.journal.read_bytes())
        assert journal["installed-sources"] == ["demo"]
        assert journal["snapshot"] == {
            "modules.json": None,
            "modules.lock.json": None,
        }
        assert json.loads(ledger.lock_info.read_bytes())["operation"] == "add"
    assert ledger.read() == declaration()
    assert ledger.read_lock() == lock()
    assert not ledger.journal.exists()
    assert not ledger.lock_info.exists()


async def test_failure_restores_exact_state_bytes_and_preserves_sources(tmp_path):
    ledger = DependencyLedger(tmp_path / "state")
    ledger.path.parent.mkdir()
    ledger.path.write_bytes(b'{ "original": true }\n')
    snapshot = ledger.snapshot()
    source = tmp_path / "modules" / "demo"
    with pytest.raises(RuntimeError, match="installation failed"):
        async with ledger.transaction("add"):
            source.mkdir(parents=True)
            ledger.record_source("demo")
            ledger.write_declaration(declaration())
            ledger.write_lock(lock())
            raise RuntimeError("installation failed")
    assert ledger.snapshot() == snapshot
    assert source.is_dir()
    assert not ledger.journal.exists()


async def test_cancellation_waits_for_snapshot_restoration(tmp_path):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(declaration())
    before = ledger.snapshot()
    entered = asyncio.Event()

    async def update():
        async with ledger.transaction("sync"):
            ledger.write_declaration({"changed": True})
            ledger.write_lock(lock())
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(update())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.snapshot() == before
    assert not ledger.journal.exists()


def interrupted_update(directory: Path, *, changed: bool):
    script = """
import asyncio
import os
import sys
from pathlib import Path
from alira.kernel.module.dependencies.ledger import DependencyLedger

async def crash():
    ledger = DependencyLedger(Path(sys.argv[1]))
    async with ledger.transaction("add"):
        if sys.argv[2] == "yes":
            ledger.write_declaration({"interrupted": True})
        os._exit(9)

asyncio.run(crash())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(directory), "yes" if changed else "no"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 9, result.stderr


async def test_crashed_update_requires_repair_and_restores_snapshot(tmp_path):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(declaration())
    original = ledger.snapshot()
    interrupted_update(tmp_path, changed=True)
    assert ledger.journal.exists()
    damaged = ledger.snapshot()
    with pytest.raises(DependencyError, match="sync --repair"):
        async with ledger.transaction("sync"):
            pytest.fail("Must not operate on an interrupted state")
    assert ledger.snapshot() == damaged
    async with ledger.transaction("sync", repair=True):
        assert ledger.snapshot() == original
    assert not ledger.journal.exists()


async def test_crashed_resolution_with_unchanged_files_can_resume(tmp_path):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(declaration())
    original = ledger.snapshot()
    interrupted_update(tmp_path, changed=False)
    async with ledger.transaction("sync"):
        assert ledger.snapshot() == original
    assert not ledger.journal.exists()


async def test_process_lock_timeout_reports_owner_and_preserves_files(tmp_path):
    script = """
import asyncio
import sys
from pathlib import Path
from alira.kernel.module.dependencies.ledger import DependencyLedger

async def hold():
    ledger = DependencyLedger(Path(sys.argv[1]))
    async with ledger.transaction("sync"):
        print("locked", flush=True)
        await asyncio.Future()

asyncio.run(hold())
"""
    process = subprocess.Popen(  # noqa: ASYNC220 - exercise an independent lock owner
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert await asyncio.to_thread(process.stdout.readline) == "locked\n"
        ledger = DependencyLedger(tmp_path, lock_timeout=0.05)
        before = ledger.snapshot()
        with pytest.raises(DependencyLockError) as caught:
            async with ledger.transaction("add"):
                pytest.fail("A competing process must not acquire the lock")
        assert caught.value.exit_code == 2
        assert f"pid {process.pid}" in str(caught.value)
        assert "operation: sync" in str(caught.value)
        assert ledger.snapshot() == before
    finally:
        process.kill()
        await asyncio.to_thread(process.communicate, timeout=5)


async def test_failed_restore_preserves_original_exception_and_journal(
    tmp_path, monkeypatch, capsys
):
    ledger = DependencyLedger(tmp_path)

    def fail_restore(snapshot):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "restore", fail_restore)
    with pytest.raises(RuntimeError, match="original"):
        async with ledger.transaction("add"):
            raise RuntimeError("original")
    assert ledger.journal.exists()
    assert "run alira lock" in capsys.readouterr().err


async def test_live_pid_does_not_mask_a_stale_journal(tmp_path):
    ledger = DependencyLedger(tmp_path)
    ledger.write_declaration(declaration())
    original = ledger.snapshot()
    interrupted_update(tmp_path, changed=True)
    journal = json.loads(ledger.journal.read_bytes())
    journal["pid"] = os.getpid()
    ledger.journal.write_text(json.dumps(journal))
    damaged = ledger.snapshot()

    with pytest.raises(DependencyError, match="sync --repair"):
        async with ledger.transaction("sync"):
            pytest.fail("A stale journal must require explicit repair")
    assert ledger.snapshot() == damaged

    async with ledger.transaction("sync", repair=True):
        assert ledger.snapshot() == original
    assert not ledger.journal.exists()
