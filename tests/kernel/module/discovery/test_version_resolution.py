"""Static deployment and startup preflight enforce the same version contracts."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from alira._version import __version__
from alira.kernel.module import BaseModule, DependencyError
from alira.kernel.module.discovery.catalog import ModuleCatalog


def declarations(tmp_path, module_manager_factory, definitions):
    manager = module_manager_factory(tmp_path)
    for name, version, required, kernel in definitions:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "main.py").write_text(
            f"module = BaseModule({name!r}, {version!r}, "
            f"required_modules={required!r}, required_kernel={kernel!r})\n"
        )
        module = BaseModule(
            name, version, required_modules=required, required_kernel=kernel
        )
        module.startup = AsyncMock()
        manager.register(module)
    return manager, ModuleCatalog(tmp_path)


@pytest.mark.parametrize(
    "requirement", ["agent>=1.0", "agent>0.9,<2", "agent~=1.0", "agent==1.*", "agent"]
)
async def test_version_requirements_resolve_to_plain_module_names(
    tmp_path, module_manager_factory, requirement
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("agent", "1.2", [], ">=3.0.0"),
            ("web", "1", [requirement], ">=3.0.0,<4"),
        ],
    )
    assert list(catalog.select(["web"])) == ["agent", "web"]
    assert manager._registry.graph(["web"]) == {"web": {"agent"}, "agent": set()}
    await manager.load("web")
    assert manager.loaded == {"agent", "web"}
    await manager.stop()


@pytest.mark.parametrize("agent_version", ["1.0", "1.5", "2.0"])
async def test_conflicting_consumers_fail_before_any_hook_or_environment_change(
    tmp_path, module_manager_factory, monkeypatch, agent_version
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("agent", agent_version, [], ">=3.0.0"),
            ("web", "1", ["agent>1.0"], ">=3.0.0"),
            ("other", "1", ["agent<=1.0"], ">=3.0.0"),
        ],
    )
    with pytest.raises(DependencyError, match="version conflict"):
        catalog.select(["web", "other"])
    install = AsyncMock(side_effect=AssertionError("unexpected installation"))
    monkeypatch.setattr(manager._dependencies, "install", install)
    with pytest.raises(DependencyError, match="version conflict"):
        await manager.load("web", "other")
    install.assert_not_awaited()
    for module in manager.modules.values():
        module.startup.assert_not_awaited()


@pytest.mark.parametrize("kernel", [">=4", "<3", f">{__version__}"])
async def test_kernel_requirement_is_checked_during_resolution(
    tmp_path, module_manager_factory, kernel
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("web", "1", [], kernel),
        ],
    )
    with pytest.raises(DependencyError, match="requires kernel"):
        catalog.select(["web"])
    with pytest.raises(DependencyError, match="requires kernel"):
        await manager.load("web")
    manager.modules["web"].startup.assert_not_awaited()


@pytest.mark.parametrize("required", [["web>=1"], ["agent>=1"]])
async def test_versioned_cycles_terminate_before_startup(
    tmp_path, module_manager_factory, required
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("web", "1", required, ">=3.0.0"),
            ("agent", "1", ["web>=1"], ">=3.0.0"),
        ],
    )
    with pytest.raises(DependencyError, match="cycle"):
        catalog.select(["web"])
    with pytest.raises(DependencyError, match="cycle"):
        await asyncio.wait_for(manager.load("web"), 1)
    for module in manager.modules.values():
        module.startup.assert_not_awaited()


@pytest.mark.parametrize(
    "requirement", ["agent=>1", "agent[extra]>=1", 'agent; python_version>"3"']
)
async def test_invalid_constraint_syntax_is_rejected(
    tmp_path, module_manager_factory, requirement
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("web", "1", [requirement], ">=3.0.0"),
        ],
    )
    with pytest.raises(DependencyError, match="Invalid module requirement"):
        catalog.select(["web"])
    with pytest.raises(DependencyError, match="Invalid module requirement"):
        await manager.load("web")


async def test_added_consumer_must_also_satisfy_retained_deployment_constraints(
    tmp_path, module_manager_factory, monkeypatch
):
    manager, catalog = declarations(
        tmp_path,
        module_manager_factory,
        [
            ("agent", "1.5", [], ">=3.0.0"),
            ("web", "1.0", ["agent>1.0"], ">=3.0.0"),
            ("other", "1.0", ["agent<=1.0"], ">=3.0.0"),
        ],
    )
    dependencies = manager._dependencies
    dependencies.ledger.write_declaration(
        {
            "format": 2,
            "requires-kernel": ">=3,<4",
            "modules": {
                name: {
                    "version": spec.version,
                    "source": {"type": "builtin"},
                    "required-modules": list(spec.required_modules),
                    "requirements": None,
                    "requirements-hash": None,
                }
                for name, spec in catalog.select(["web"]).items()
            },
        }
    )
    before = dependencies.ledger.snapshot()
    resolution = AsyncMock(
        side_effect=AssertionError("Python resolution must not start")
    )
    monkeypatch.setattr(
        "alira.kernel.module.dependencies.deployment.resolve_requirements", resolution
    )
    with pytest.raises(DependencyError, match="version conflict"):
        await manager.add("other", no_sync=True)
    resolution.assert_not_awaited()
    assert dependencies.ledger.snapshot() == before


def test_kernel_requirement_defaults_and_static_version_fields(tmp_path):
    from alira.kernel.module.discovery.catalog import ModuleSpec

    assert BaseModule("demo", "1").required_kernel == ">=3.0.0"
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "main.py").write_text(
        "VERSION='1.2.3'\nmodule=BaseModule('demo', VERSION, required_kernel='>=3,<4')"
    )
    spec = ModuleSpec.read(folder)
    assert spec.version == "1.2.3"
    assert spec.required_kernel == ">=3,<4"


def test_large_cycle_is_reported_without_python_recursion():
    from alira.kernel.module.discovery.resolution import resolve_module_graph

    modules = {
        f"m{i}": BaseModule(f"m{i}", "1", required_modules=[f"m{(i + 1) % 2000}"])
        for i in range(2000)
    }
    with pytest.raises(DependencyError, match="cycle"):
        resolve_module_graph(["m0"], modules.__getitem__)
