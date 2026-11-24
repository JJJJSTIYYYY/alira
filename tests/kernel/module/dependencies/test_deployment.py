"""Declaration resolution and exact artifact installation are separate phases."""

import importlib
import platform
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alira._version import __version__
from alira.kernel.module.dependencies.requirements import requirements_fingerprint
from alira.kernel.module.dependencies.types import (
    DependencyError,
    DependencyInstallError,
    DependencyResolutionError,
    ModuleRequirements,
)
from alira.kernel.module.dependencies.uv import installation_lock
from alira.kernel.module.discovery.catalog import ModuleCatalog

code = importlib.import_module("alira.kernel.module.dependencies.deployment")


def package(name, version="1.0"):
    return {
        "name": name,
        "version": version,
        "wheels": [
            {
                "url": f"https://example.test/{name}-{version}-py3-none-any.whl",
                "hashes": {"sha256": "a" * 64},
            }
        ],
    }


@pytest.mark.parametrize("with_uv", [False, True])
def test_kernel_protection_includes_installed_dependency_tool(monkeypatch, with_uv):
    versions = {"alira-harness": "1.0", "host-runtime": "4.0", "plugin-extra": "2.0"}
    if with_uv:
        versions["uv"] = "0.9.7"
    monkeypatch.setattr(
        code,
        "distributions",
        lambda: [
            SimpleNamespace(
                metadata={"Name": name},
                version=version,
                requires=["host-runtime>=4"] if name == "alira-harness" else [],
            )
            for name, version in versions.items()
        ],
    )
    assert code.kernel_packages() == {
        name: version for name, version in versions.items() if name != "plugin-extra"
    }


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "modules"
    root.mkdir()
    for name in ("left", "right"):
        source = root / name
        source.mkdir()
        (source / "main.py").write_text(f"module = BaseModule({name!r}, '1.0')\n")
        (source / "requirements.txt").write_text(f"{name}-pkg\n")
    specs = ModuleCatalog(root).select(["left", "right"])
    declaration = {
        "format": 2,
        "requires-kernel": ">=3,<4",
        "modules": {
            name: {
                "version": spec.version,
                "source": {"type": "builtin"},
                "required-modules": [],
                "requirements": "requirements.txt",
                "requirements-hash": "sha256:"
                + requirements_fingerprint(spec.requirements),
            }
            for name, spec in specs.items()
        },
    }
    dependency = code.ModuleDependencies(tmp_path / "state")
    monkeypatch.setattr(
        code,
        "kernel_packages",
        lambda: {"alira-harness": __version__, "kernel-runtime": "1.0"},
    )
    resolved_calls = []

    async def resolve(sources, pins, directory, cwd, *, previous_lock=None):
        resolved_calls.append((sources, pins, previous_lock))
        packages = {
            name: package(name)
            for name in ("left-pkg", "right-pkg", "shared-pkg", "kernel-runtime")
        }
        combined = directory / "combined"
        combined.mkdir()
        (combined / "pylock.toml").write_bytes(
            installation_lock({"packages": packages})
        )
        return {
            name: ModuleRequirements(
                None,
                {f"{name}-pkg": "1.0", "shared-pkg": "1.0", "kernel-runtime": "1.0"},
            )
            for name in sources
        }

    monkeypatch.setattr(code, "resolve_requirements", resolve)
    return SimpleNamespace(
        dependency=dependency,
        root=root,
        declaration=declaration,
        specs=specs,
        calls=resolved_calls,
    )


async def test_resolution_does_not_write_or_install_and_tracks_shared_closures(
    deployment, monkeypatch
):
    env = deployment
    monkeypatch.setattr(
        code,
        "run_uv",
        AsyncMock(side_effect=AssertionError("resolution installed packages")),
    )
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    assert not env.dependency.ledger.directory.exists()
    assert lock["kernel"] == {
        "version": __version__,
        "python": platform.python_version(),
    }
    assert set(lock["packages"]) == {"left-pkg", "right-pkg", "shared-pkg"}
    assert lock["modules"]["left"]["packages"] == ["left-pkg", "shared-pkg"]
    assert env.calls[0][1]["kernel-runtime"] == "1.0"
    env.dependency.ledger.write_declaration(env.declaration)
    env.dependency.ledger.write_lock(lock)
    assert env.dependency.info["shared-pkg"].modules == {"left", "right"}


async def test_existing_fresh_lock_is_reused_without_resolving(deployment):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    env.dependency.ledger.write_lock(lock)
    assert (
        await env.dependency.resolve(env.declaration, env.specs, cwd=env.root) == lock
    )
    assert len(env.calls) == 1


async def test_changed_inputs_use_previous_lock_as_preference(deployment):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    env.dependency.ledger.write_lock(lock)
    env.declaration["requires-kernel"] = ">=3.0.1,<4"
    updated = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    assert updated["inputs-hash"] != lock["inputs-hash"]
    assert env.calls[-1][2] == lock


async def test_current_check_detects_recursive_requirement_and_source_changes(
    deployment,
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    assert env.dependency.lock_is_current(env.declaration, env.specs, lock=lock)
    (env.root / "left" / "new_source.py").write_text("value = 1\n")
    assert not env.dependency.lock_is_current(env.declaration, env.specs, lock=lock)
    (env.root / "left" / "new_source.py").unlink()
    requirement = env.root / "left" / "requirements.txt"
    requirement.write_text("-r extra.txt\n")
    (requirement.parent / "extra.txt").write_text("different-package\n")
    assert not env.dependency.lock_is_current(env.declaration, env.specs, lock=lock)


async def test_resolution_failure_leaves_existing_state_untouched(
    deployment, monkeypatch
):
    env = deployment
    env.dependency.ledger.write_declaration(env.declaration)
    before = env.dependency.ledger.snapshot()
    monkeypatch.setattr(
        code,
        "resolve_requirements",
        AsyncMock(side_effect=DependencyError("No solution found")),
    )
    with pytest.raises(DependencyResolutionError, match="No solution found"):
        await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    assert env.dependency.ledger.snapshot() == before


async def test_locked_source_restoration_accepts_missing_tree_but_checks_existing_sources(
    deployment,
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    shutil.rmtree(env.specs["left"].directory)
    remaining = {"right": env.specs["right"]}
    assert not env.dependency.lock_is_current(env.declaration, remaining, lock=lock)
    assert env.dependency.lock_is_current(
        env.declaration, remaining, lock=lock, allow_missing_sources=True
    )
    (env.specs["right"].directory / "changed.py").write_text("changed = True\n")
    assert not env.dependency.lock_is_current(
        env.declaration, remaining, lock=lock, allow_missing_sources=True
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "2.0"),
        ("required-modules", ["other"]),
        ("requirements-hash", "sha256:changed"),
    ],
)
async def test_missing_sources_still_require_matching_locked_metadata(
    deployment, field, value
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    lock["modules"]["left"][field] = value
    assert not env.dependency.lock_is_current(
        env.declaration, {}, lock=lock, allow_missing_sources=True
    )


async def test_install_consumes_only_exact_hashed_lock_and_temp_artifact(
    deployment, monkeypatch
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    for spec in env.specs.values():
        spec.requirements.unlink()
    paths = []

    async def uv(*arguments, **kwargs):
        assert arguments[0] == "install"
        assert "--no-deps" in arguments
        path = Path(arguments[arguments.index("--requirements") + 1])
        paths.append(path)
        artifact = tomllib.loads(path.read_text())
        assert artifact["packages"] == [
            {
                key: value
                for key, value in lock["packages"][name].items()
                if key != "sha256"
            }
            for name in sorted(lock["packages"])
        ]

    monkeypatch.setattr(code, "run_uv", uv)
    monkeypatch.setattr(code, "_installed_distributions", lambda: ({}, set()))
    await env.dependency.install(lock, cwd=env.root, prune=False)
    assert paths and not paths[0].exists()
    assert not env.dependency.ledger.directory.exists()


@pytest.mark.parametrize("prune", [False, True])
async def test_exact_sync_prunes_extras_but_run_preserves_them(
    deployment, monkeypatch, prune
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    monkeypatch.setattr(code, "run_uv", AsyncMock())
    monkeypatch.setattr(code, "_installed_distributions", lambda: ({}, set()))
    monkeypatch.setattr(
        env.dependency,
        "installed",
        lambda: {
            "left-pkg": "1.0",
            "shared-pkg": "1.0",
            "unrelated": "1.0",
            "kernel-runtime": "1.0",
        },
    )
    uninstall = AsyncMock()
    monkeypatch.setattr(env.dependency, "uninstall_packages", uninstall)
    await env.dependency.install(lock, cwd=env.root, prune=prune)
    if prune:
        assert uninstall.call_args.args == ({"unrelated"},)
    else:
        uninstall.assert_not_called()


async def test_tampered_artifact_hash_fails_before_installation(
    deployment, monkeypatch
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    lock["packages"]["left-pkg"]["wheels"][0]["hashes"]["sha256"] = "b" * 64
    uv = AsyncMock()
    monkeypatch.setattr(code, "run_uv", uv)
    with pytest.raises(DependencyInstallError, match="fingerprint") as error:
        await env.dependency.install(lock, cwd=env.root)
    assert error.value.exit_code == 3
    uv.assert_not_called()


async def test_missing_artifact_hash_is_rejected_during_resolution(
    deployment, monkeypatch
):
    env = deployment
    original = code.resolve_requirements

    async def unhashed(*args, **kwargs):
        plans = await original(*args, **kwargs)
        path = args[2] / "combined" / "pylock.toml"
        path.write_text(
            path.read_text().replace(
                '"sha256" = "' + "a" * 64 + '"', '"sha512" = "abc"'
            )
        )
        return plans

    monkeypatch.setattr(code, "resolve_requirements", unhashed)
    with pytest.raises(DependencyResolutionError, match="SHA-256"):
        await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)


async def test_install_failure_keeps_lock_and_has_install_exit_code(
    deployment, monkeypatch
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    env.dependency.ledger.write_lock(lock)
    before = env.dependency.ledger.snapshot()
    monkeypatch.setattr(
        code, "run_uv", AsyncMock(side_effect=DependencyError("download failed"))
    )
    with pytest.raises(DependencyInstallError, match="download failed") as error:
        await env.dependency.install(lock, cwd=env.root)
    assert error.value.exit_code == 3
    assert env.dependency.ledger.snapshot() == before


async def test_project_kernel_constraint_is_checked_before_resolver(deployment):
    env = deployment
    env.declaration["requires-kernel"] = ">=99"
    with pytest.raises(DependencyResolutionError, match="Project requires kernel"):
        await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    assert not env.calls


async def test_legacy_migration_preserves_artifacts_and_ownership_without_resolving(
    deployment, monkeypatch
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    records = {
        name: {
            "packages": {
                package: lock["packages"][package]["version"]
                for package in record["packages"]
            },
            "requirements_hash": requirements_fingerprint(env.specs[name].requirements),
            "version": env.specs[name].version,
            "required_modules": list(env.specs[name].required_modules),
        }
        for name, record in lock["modules"].items()
    }
    resolver = AsyncMock(side_effect=AssertionError("migration resolved packages"))
    monkeypatch.setattr(code, "resolve_requirements", resolver)
    migrated = env.dependency.migrate_lock(
        env.declaration, env.specs, records, installation_lock(lock)
    )
    assert migrated == lock
    resolver.assert_not_called()


@pytest.mark.parametrize("changed", ["requirements", "version", "required_modules"])
async def test_legacy_migration_rejects_changed_inputs_instead_of_blessing_old_resolution(
    deployment, changed
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    records = {
        name: {
            "packages": {
                package: lock["packages"][package]["version"]
                for package in record["packages"]
            },
            "requirements_hash": requirements_fingerprint(env.specs[name].requirements),
            "version": env.specs[name].version,
            "required_modules": list(env.specs[name].required_modules),
        }
        for name, record in lock["modules"].items()
    }
    if changed == "requirements":
        env.specs["left"].requirements.write_text("new-package-never-resolved==1.0\n")
    elif changed == "version":
        records["left"]["version"] = "0.9"
    else:
        records["left"]["required_modules"] = ["old_prerequisite"]
    with pytest.raises(
        DependencyResolutionError, match="Legacy inputs changed.*alira add left"
    ):
        env.dependency.migrate_lock(
            env.declaration, env.specs, records, installation_lock(lock)
        )
    assert not env.dependency.ledger.directory.exists()


async def test_uninstall_failure_uses_install_exit_code(deployment, monkeypatch):
    monkeypatch.setattr(code, "_release_kernel_file_claims", lambda *args: None)
    monkeypatch.setattr(
        code, "run_uv", AsyncMock(side_effect=DependencyError("uninstall failed"))
    )
    with pytest.raises(DependencyInstallError, match="uninstall failed") as error:
        await deployment.dependency.uninstall_packages({"orphan"}, cwd=deployment.root)
    assert error.value.exit_code == 3


@pytest.mark.parametrize("protected_version", ["1.0", "2.0"])
async def test_legacy_kernel_pins_must_match_current_kernel_before_exclusion(
    deployment, monkeypatch, protected_version
):
    env = deployment
    lock = await env.dependency.resolve(env.declaration, env.specs, cwd=env.root)
    records = {
        name: {
            "packages": {
                package: lock["packages"][package]["version"]
                for package in record["packages"]
            },
            "requirements_hash": requirements_fingerprint(env.specs[name].requirements),
        }
        for name, record in lock["modules"].items()
    }
    records["left"]["packages"]["kernel-runtime"] = "1.0"
    protected = {
        "name": "kernel-runtime",
        "version": "1.0",
        "wheels": [{"path": "removed-kernel-wheel.whl", "hashes": {}}],
    }
    artifact = installation_lock(
        lock | {"packages": lock["packages"] | {"kernel-runtime": protected}}
    )
    monkeypatch.setattr(
        code, "kernel_packages", lambda: {"kernel-runtime": protected_version}
    )
    if protected_version == "2.0":
        with pytest.raises(
            DependencyResolutionError, match="Legacy lock requires kernel-runtime==1.0"
        ):
            env.dependency.migrate_lock(env.declaration, env.specs, records, artifact)
    else:
        migrated = env.dependency.migrate_lock(
            env.declaration, env.specs, records, artifact
        )
        assert "kernel-runtime" not in migrated["packages"]
